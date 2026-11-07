"""后端子进程管理。

负责：
1. 通过 QProcess 启动 adsb_display 独立进程组；
2. 监视标准输出解析 ADSB_READY 协议；
3. 异步请求 /api/meta 校验 session_id；
4. 60秒启动超时与启动失败检测；
5. 运行期间崩溃检测与遗留子进程清理；
6. 优雅退出流程：/api/system/shutdown -> 30s超时 -> SIGTERM -> 5s超时 -> SIGKILL。
"""
from __future__ import annotations

import glob
import json
import logging
import os
import signal
import socket
import sys
import threading
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

from .constants import (
    BACKEND_DIR,
    DESKTOP_LOGS_DIR,
    PROJECT_ROOT,
    SHUTDOWN_GRACE_SECONDS,
    SHUTDOWN_KILL_SECONDS,
    STARTUP_TIMEOUT_SECONDS,
)

log = logging.getLogger("adsb.desktop.backend")


def is_port_available(host: str, port: int) -> bool:
    """检查指定端口是否可用（允许复用处于 TIME_WAIT 状态的端口）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


class BackendProcessManager(QObject):
    ready = Signal(str, dict)                     # (url, ready_info)
    startup_failed = Signal(str)                  # (error_message)
    crashed = Signal(int, str)                    # (exit_code, error_message)
    shutdown_completed = Signal()                 # ()

    # 内部跨线程信号 (携带代次标记，防止旧会话回调污染新会话)
    _verify_success_signal = Signal(int, str, dict)
    _verify_failed_signal = Signal(int, str)
    _cleanup_completed_signal = Signal(int)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        DESKTOP_LOGS_DIR.mkdir(parents=True, exist_ok=True)

        self._verify_success_signal.connect(self._on_verify_success)
        self._verify_failed_signal.connect(self._on_verify_failed)
        self._cleanup_completed_signal.connect(self._on_cleanup_completed)

        self._proc: QProcess | None = None
        self._pid: int | None = None
        self._pgid: int | None = None
        self._port: int | None = None
        self._host: str = "127.0.0.1"
        self._current_generation: int = 0
        self._cleaning_pgids: set[int] = set()
        self._is_ready = False
        self._is_shutting_down = False
        self._ready_info: dict | None = None
        self._startup_failed_emitted = False
        self._shutdown_completed_emitted = False
        self._session_id: str | None = None

        self.shutdown_grace_seconds: float = float(SHUTDOWN_GRACE_SECONDS)
        self.shutdown_kill_seconds: float = float(SHUTDOWN_KILL_SECONDS)
        self._shutdown_gen: int = 0
        self._shutdown_pgid: int | None = None
        self._shutdown_proc: QProcess | None = None

        self._stdout_buffer = ""
        self._stderr_lines: list[str] = []

        # 启动超时定时器
        self._startup_timer = QTimer(self)
        self._startup_timer.setSingleShot(True)
        self._startup_timer.timeout.connect(self._on_startup_timeout)

        # 优雅关闭定时器
        self._grace_timer = QTimer(self)
        self._grace_timer.setSingleShot(True)
        self._grace_timer.timeout.connect(self._on_grace_timeout)

        self._kill_timer = QTimer(self)
        self._kill_timer.setSingleShot(True)
        self._kill_timer.timeout.connect(self._on_kill_timeout)

    @property
    def pid(self) -> int | None:
        return self._pid

    @property
    def pgid(self) -> int | None:
        return self._pgid

    @property
    def is_ready(self) -> bool:
        return self._is_ready

    @property
    def ready_info(self) -> dict | None:
        return self._ready_info

    @property
    def session_id(self) -> str | None:
        return self._session_id

    def is_running(self) -> bool:
        """检查后端主进程或所属进程组（包含当前和关闭中的会话）是否在运行。"""
        proc_alive = (
            (self._proc is not None and self._proc.state() != QProcess.ProcessState.NotRunning)
            or (self._shutdown_proc is not None and self._shutdown_proc.state() != QProcess.ProcessState.NotRunning)
        )
        pgid_alive = self._is_pgid_alive(self._pgid) or self._is_pgid_alive(self._shutdown_pgid)
        return proc_alive or pgid_alive

    def is_cleanup_completed(self) -> bool:
        """检查所有主进程及所属进程组（包括当前及关闭中的代次）是否已彻底消亡。"""
        return not self.is_running()

    def start(
        self,
        mode: str = "auto",
        port: int = 18080,
        dataset: str | None = None,
        duration: int = 0,
        lat: float | None = None,
        lon: float | None = None,
        gain: float | None = None,
    ) -> bool:
        """启动后端子进程。"""
        self._is_ready = False
        self._is_shutting_down = False
        self._startup_failed_emitted = False
        self._shutdown_completed_emitted = False
        self._session_id = None
        self._ready_info = None
        self._stdout_buffer = ""
        self._stderr_lines.clear()
        self._port = port
        self._host = "127.0.0.1"

        # 递增启动代次，使旧会话的校验线程、定时器与回调失效
        self._current_generation += 1
        self._grace_timer.stop()
        self._kill_timer.stop()
        self._startup_timer.stop()
        self._shutdown_gen = 0
        self._shutdown_pgid = None
        self._shutdown_proc = None

        # 如果存在旧进程或旧进程组，彻底清理并断开信号，防止旧进程/孤儿进程干扰新进程
        if self._proc is not None or self._pgid is not None:
            old_proc = self._proc
            old_pgid = self._pgid
            if old_proc is not None:
                try:
                    old_proc.readyReadStandardOutput.disconnect()
                    old_proc.readyReadStandardError.disconnect()
                    old_proc.finished.disconnect()
                    old_proc.errorOccurred.disconnect()
                except Exception:
                    pass
                if old_proc.state() != QProcess.ProcessState.NotRunning:
                    old_proc.kill()
                    old_proc.waitForFinished(300)
                old_proc.deleteLater()
            if old_pgid is not None:
                # 快速发送 SIGKILL 解除可能残留的端口或设备占用，并同步收尾确保完全消亡
                try:
                    if old_pgid > 1 and old_pgid != os.getpgrp():
                        os.killpg(old_pgid, signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
                self._cleanup_specific_pgid(old_pgid, synchronous=True)
            self._proc = None
            self._pid = None
            self._pgid = None

        # 检查端口占用
        if not is_port_available(self._host, self._port):
            msg = f"HTTP 端口 {self._port} 已被占用，无法启动桌面服务。请先关闭占用该端口的程序。"
            log.error(msg)
            QTimer.singleShot(0, lambda: self._emit_startup_failed(msg))
            return False

        args = [
            "-m", "adsb_display",
            "serve",
            "--mode", mode,
            "--host", self._host,
            "--port", str(self._port),
            "--ready-json",
            "--desktop-managed",
        ]
        if dataset:
            args.extend(["--dataset", dataset])
        if duration and duration > 0:
            args.extend(["--duration", str(duration)])
        if lat is not None:
            args.extend(["--lat", str(lat)])
        if lon is not None:
            args.extend(["--lon", str(lon)])
        if gain is not None:
            args.extend(["--gain", str(gain)])

        self._proc = QProcess(self)
        self._proc.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self._proc.setWorkingDirectory(str(PROJECT_ROOT))

        # 环境变量
        env = QProcessEnvironment.systemEnvironment()
        existing_py = env.value("PYTHONPATH", "")
        backend_path = str(BACKEND_DIR)
        env.insert("PYTHONPATH", f"{backend_path}:{existing_py}" if existing_py else backend_path)
        env.insert("PYTHONUNBUFFERED", "1")
        self._proc.setProcessEnvironment(env)

        # 建立独立进程组 / 会话
        params = QProcess.UnixProcessParameters()
        params.flags |= QProcess.UnixProcessFlag.CreateNewSession
        self._proc.setUnixProcessParameters(params)

        # 连接信号
        self._proc.readyReadStandardOutput.connect(self._on_stdout_ready)
        self._proc.readyReadStandardError.connect(self._on_stderr_ready)
        self._proc.finished.connect(self._on_proc_finished)
        self._proc.errorOccurred.connect(self._on_proc_error)

        py_bin = sys.executable
        log.info("启动后端子进程: %s %s", py_bin, " ".join(args))
        self._proc.start(py_bin, args)

        if not self._proc.waitForStarted(3000):
            err = self._proc.errorString()
            msg = f"启动后端子进程失败: {err}"
            log.error(msg)
            QTimer.singleShot(0, lambda: self._emit_startup_failed(msg))
            return False

        self._pid = self._proc.processId()
        self._pgid = self._pid  # 由于 CreateNewSession，PGID 与 PID 一致
        log.info("后端子进程已启动 pid=%s, pgid=%s", self._pid, self._pgid)

        # 启动 60 秒超时计时
        self._startup_timer.start(STARTUP_TIMEOUT_SECONDS * 1000)
        return True

    def _on_stdout_ready(self) -> None:
        if not self._proc:
            return
        data = self._proc.readAllStandardOutput().data().decode("utf-8", errors="replace")
        self._stdout_buffer += data
        while "\n" in self._stdout_buffer:
            line, self._stdout_buffer = self._stdout_buffer.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            log.debug("[backend stdout] %s", line)
            if not self._is_ready and line.startswith("ADSB_READY "):
                self._handle_ready_json(line[len("ADSB_READY "):].strip(), generation=self._current_generation)

    def _on_stderr_ready(self) -> None:
        if not self._proc:
            return
        data = self._proc.readAllStandardError().data().decode("utf-8", errors="replace")
        for line in data.splitlines():
            line_str = line.strip()
            if line_str:
                log.warning("[backend stderr] %s", line_str)
                self._stderr_lines.append(line_str)
                if len(self._stderr_lines) > 200:
                    self._stderr_lines.pop(0)

    def _emit_startup_failed(self, reason: str) -> None:
        if self._startup_failed_emitted or self._is_ready:
            return
        self._startup_failed_emitted = True
        # 立即使当前代次失效，防止并发/迟到的校验线程反向覆盖
        self._current_generation += 1
        self._startup_timer.stop()
        self._cleanup_process_group(synchronous=False)
        self.startup_failed.emit(reason)

    def _emit_shutdown_completed(self) -> None:
        if self._shutdown_completed_emitted:
            return
        self._shutdown_completed_emitted = True
        self._grace_timer.stop()
        self._kill_timer.stop()
        self._shutdown_gen = 0
        self._shutdown_proc = None
        self._shutdown_pgid = None
        self.shutdown_completed.emit()

    def _check_and_emit_shutdown_completed(self) -> None:
        """统一确认主进程已结束且所属进程组已完全清理后，再发送最终完成信号。"""
        if not self._is_shutting_down:
            return
        if self._shutdown_completed_emitted:
            return

        target_proc = self._shutdown_proc if self._shutdown_proc is not None else self._proc
        target_pgid = self._shutdown_pgid if self._shutdown_pgid is not None else self._pgid

        # 1. 检查主进程是否已停止运行
        proc_running = (
            (target_proc is not None and target_proc.state() != QProcess.ProcessState.NotRunning)
            or (self._proc is not None and self._proc.state() != QProcess.ProcessState.NotRunning)
        )
        if proc_running:
            log.debug("主进程仍在运行，暂不宣告退出完成")
            return

        # 2. 检查所属进程组是否仍有存活子进程
        if self._is_pgid_alive(target_pgid) or self._is_pgid_alive(self._pgid):
            alive_pgid = target_pgid if self._is_pgid_alive(target_pgid) else self._pgid
            log.debug("所属进程组 (PGID=%s) 仍有存活子进程，等待异步清理收尾…", alive_pgid)
            if alive_pgid and alive_pgid not in self._cleaning_pgids:
                self._cleanup_specific_pgid(alive_pgid, synchronous=False)
            return

        # 3. 确认所有子进程及主进程均已完全终止，宣告收尾完成
        log.info("主进程及所属进程组已全部完全终止，发出 shutdown_completed 信号")
        self._emit_shutdown_completed()

    def _handle_ready_json(self, json_str: str, generation: int | None = None) -> None:
        gen = generation if generation is not None else self._current_generation
        log.info("收到后端就绪通知单行 JSON (gen=%d): %s", gen, json_str)
        try:
            info = json.loads(json_str)
            expected_session = info.get("session_id")
            self._session_id = expected_session
            host = info.get("host", self._host)
            port = info.get("port", self._port)
            url = f"http://{host}:{port}"
        except Exception as e:
            log.error("解析 ADSB_READY JSON 失败: %s", e)
            self._emit_startup_failed(f"解析后端就绪协议失败: {e}")
            return

        # 异步线程校验 /api/meta 中的 session_id (绑定代次 gen)
        def _verify():
            if gen != self._current_generation or self._startup_failed_emitted or self._is_shutting_down:
                log.info("会话校验线程已废弃 (gen=%d != current=%d)，退出", gen, self._current_generation)
                return
            meta_url = f"{url}/api/meta"
            for attempt in range(10):
                if gen != self._current_generation or self._startup_failed_emitted or self._is_shutting_down:
                    log.info("会话校验线程已废弃 (gen=%d != current=%d)，退出", gen, self._current_generation)
                    return
                try:
                    req = Request(meta_url, headers={"User-Agent": "adsb-desktop"})
                    with urlopen(req, timeout=3) as resp:
                        if resp.status == 200:
                            meta_data = json.loads(resp.read().decode("utf-8"))
                            actual_session = meta_data.get("session_id")
                            if actual_session == expected_session:
                                log.info("后端 session_id 校验通过: %s (gen=%d)", actual_session, gen)
                                self._verify_success_signal.emit(gen, url, info)
                                return
                            else:
                                log.error("session_id 不匹配: expect=%s, actual=%s (gen=%d)", expected_session, actual_session, gen)
                                self._verify_failed_signal.emit(gen, "后端 session_id 校验不匹配")
                                return
                except (URLError, OSError) as exc:
                    time.sleep(0.3)
            self._verify_failed_signal.emit(gen, "请求 /api/meta 超时，未能校验会话就绪状态")

        threading.Thread(target=_verify, name=f"adsb-meta-verify-gen{gen}", daemon=True).start()

    def _on_verify_success(self, gen: int, url: str, info: dict) -> None:
        # 1. 检查代次有效性
        if gen != self._current_generation:
            log.info("忽略已废弃代次的会话校验成功信号: gen=%d, current=%d", gen, self._current_generation)
            return

        # 2. 检查终止/失败状态：若已失败或已进入退出流程，直接丢弃迟到的成功信号
        if self._startup_failed_emitted or self._is_shutting_down:
            log.warning("启动已失败或正在退出，丢弃迟到的会话校验成功信号 (gen=%d)", gen)
            return

        # 3. 检查后端进程是否仍在运行：若已处于非运行状态，绝对不能发出 ready 覆盖错误页面
        if self._proc is not None and self._proc.state() != QProcess.ProcessState.Running:
            log.warning("后端进程当前非运行状态 (%s)，丢弃迟到的会话校验成功信号 (gen=%d)",
                        self._proc.state(), gen)
            return

        self._startup_timer.stop()
        self._is_ready = True
        self._ready_info = info
        self.ready.emit(url, info)

    def _on_verify_failed(self, gen: int, reason: str) -> None:
        if gen != self._current_generation:
            log.info("忽略已废弃代次的会话校验失败信号: gen=%d, current=%d, reason=%s", gen, self._current_generation, reason)
            return
        if self._startup_failed_emitted or self._is_shutting_down:
            return
        self._emit_startup_failed(reason)

    def _on_startup_timeout(self) -> None:
        log.error("后端启动超时（超过 %d 秒）", STARTUP_TIMEOUT_SECONDS)
        self._current_generation += 1
        stderr_tail = "\n".join(self._stderr_lines[-10:])
        msg = f"后端服务启动超时（等待超过 {STARTUP_TIMEOUT_SECONDS} 秒）。\n{stderr_tail}"
        self._emit_startup_failed(msg)

    def _on_proc_finished(self, exit_code: int, exit_status: QProcess.ExitStatus) -> None:
        if self.sender() is not None and self.sender() is not self._proc:
            log.info("忽略已废弃的旧进程退出信号 (sender mismatch)")
            return

        log.info("后端子进程已退出 exit_code=%d, exit_status=%s", exit_code, exit_status)

        # 无论是否已就绪，若已处于退出流程中，触发进程组清理并在确认全部消亡后完成收尾
        if self._is_shutting_down:
            self._cleanup_process_group(synchronous=False)
            self._check_and_emit_shutdown_completed()
            return

        if not self._is_ready:
            self._current_generation += 1
            stderr_tail = "\n".join(self._stderr_lines[-15:])
            msg = f"后端在就绪前退出 (代码 {exit_code})。\n{stderr_tail}"
            self._emit_startup_failed(msg)
            return

        # 后端正常结束后退出桌面应用 (例如定时时长结束或页面终止系统)
        if exit_code == 0 and exit_status == QProcess.ExitStatus.NormalExit:
            log.info("后端子进程正常结束 (exit_code=0)，触发桌面端正常退出流程。")
            self._is_shutting_down = True
            self._cleanup_process_group(synchronous=False)
            self._check_and_emit_shutdown_completed()
            return

        # 运行中异常结束保留错误窗口，提供“重试”和“退出”，不自动循环重启
        self._cleanup_process_group(synchronous=False)
        stderr_tail = "\n".join(self._stderr_lines[-15:])
        msg = f"后端服务意外终止 (代码 {exit_code})。\n{stderr_tail}"
        self.crashed.emit(exit_code, msg)

    def _on_proc_error(self, err: QProcess.ProcessError) -> None:
        log.error("后端进程错误: %s", err)
        if self._is_shutting_down:
            log.info("退出流程中收到进程错误 (%s)，等待所属进程组完全清理后再收尾", err)
            self._cleanup_process_group(synchronous=False)
            self._check_and_emit_shutdown_completed()
            return
        if not self._is_ready:
            self._current_generation += 1
            self._emit_startup_failed(f"后端进程启动或运行错误: {err}")

    def stop(self, timeout: float = 2.0) -> None:
        """安全停止后端进程（便利方法）。"""
        self.request_shutdown()

    def force_kill_and_cleanup(self, synchronous: bool = True) -> bool:
        """强制终止当前主进程及其所属进程组，取消常规超时升级，直接发送 SIGKILL 并彻底清理。
        返回布尔值：若已确认主进程及所属进程组均完全终止则返回 True，否则返回 False。
        """
        self._grace_timer.stop()
        self._kill_timer.stop()
        self._startup_timer.stop()
        self._is_shutting_down = True

        target_proc = self._shutdown_proc or self._proc
        target_pgid = self._shutdown_pgid or self._pgid

        log.warning("强制清理并强杀后端进程组 (PGID=%s)…", target_pgid)

        # 1. 终止主进程
        if target_proc is not None:
            try:
                if target_proc.state() != QProcess.ProcessState.NotRunning:
                    target_proc.kill()
                    target_proc.waitForFinished(500)
            except Exception:
                pass

        # 2. 向进程组发送 SIGKILL
        if target_pgid is not None and target_pgid > 1 and target_pgid != os.getpgrp():
            try:
                os.killpg(target_pgid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass

        # 3. 清理残留进程并等待完全消亡
        if target_pgid is not None:
            self._cleanup_specific_pgid(
                target_pgid,
                term_wait_sec=0.1,
                kill_wait_sec=0.5,
                synchronous=synchronous,
            )

        self._check_and_emit_shutdown_completed()
        return self.is_cleanup_completed()

    def request_shutdown(self) -> None:
        """发起异步优雅关闭。不阻塞 Qt 主线程。"""
        if self._is_shutting_down:
            if self._shutdown_completed_emitted:
                log.debug("后端已完成退出，重新发出 shutdown_completed 信号")
                QTimer.singleShot(0, self.shutdown_completed.emit)
            return
        self._is_shutting_down = True
        self._startup_timer.stop()

        self._shutdown_gen = self._current_generation
        self._shutdown_pgid = self._pgid
        self._shutdown_proc = self._proc
        req_gen = self._current_generation
        target_pgid = self._pgid
        target_proc = self._proc

        proc_alive = self._proc is not None and self._proc.state() != QProcess.ProcessState.NotRunning
        pgid_alive = self._is_pgid_alive(self._pgid)

        if not proc_alive and not pgid_alive:
            log.info("后端进程及进程组当前未运行，直接完成退出流程。")
            self._shutdown_completed_emitted = True
            QTimer.singleShot(0, self.shutdown_completed.emit)
            return

        if not proc_alive and pgid_alive:
            log.warning("后端主进程已退出但进程组 %s 仍有残留子进程，执行异步清理收尾…", self._pgid)
            self._cleanup_process_group(synchronous=False)
            self._check_and_emit_shutdown_completed()
            return

        log.info("开始优雅退出流程 (gen=%d, pgid=%s)…", req_gen, target_pgid)

        sid = self._session_id or ""

        def _call_shutdown():
            if self._current_generation != req_gen:
                log.info("关闭请求代次已废弃 (req=%d, current=%d)，放弃发送请求", req_gen, self._current_generation)
                return
            if self._port and self._is_ready:
                url = f"http://{self._host}:{self._port}/api/system/shutdown"
                payload = json.dumps({"session_id": sid}).encode("utf-8")
                headers = {
                    "Content-Type": "application/json",
                    "X-Session-ID": sid,
                }
                try:
                    req = Request(url, data=payload, headers=headers)
                    with urlopen(req, timeout=3) as resp:
                        log.info("/api/system/shutdown 请求已发送: status=%s (session=%s)", resp.status, sid)
                except Exception as exc:
                    log.warning("向后端发送 shutdown 请求失败 (可能已在退出中): %s", exc)
            else:
                log.info("后端尚未就绪即收到退出请求，向进程组发送 SIGTERM 加速终止…")
                self._send_signal_to_target_pgid(target_pgid, target_proc, signal.SIGTERM)

        threading.Thread(target=_call_shutdown, name="adsb-shutdown-req", daemon=True).start()

        # 启动优雅退出定时器（默认 30 秒，支持测试缩短）
        self._grace_timer.start(int(self.shutdown_grace_seconds * 1000))

    def _on_grace_timeout(self) -> None:
        if (self._shutdown_gen != 0 and self._shutdown_gen != self._current_generation) or not self._is_shutting_down:
            log.info("忽略已废弃代次的优雅退出超时 (shutdown_gen=%d != current=%d)",
                     self._shutdown_gen, self._current_generation)
            return
        target_pgid = self._shutdown_pgid if self._shutdown_pgid is not None else self._pgid
        target_proc = self._shutdown_proc if self._shutdown_proc is not None else self._proc
        proc_alive = target_proc is not None and target_proc.state() != QProcess.ProcessState.NotRunning
        pgid_alive = self._is_pgid_alive(target_pgid)
        if not proc_alive and not pgid_alive:
            self._check_and_emit_shutdown_completed()
            return
        log.warning("后端在收到优雅退出请求后 %s 秒内未退出，向目标进程组 (PGID=%s) 发送 SIGTERM",
                    self.shutdown_grace_seconds, target_pgid)
        self._send_signal_to_target_pgid(target_pgid, target_proc, signal.SIGTERM)
        # 启动强制终止定时器（默认 5 秒，支持测试缩短）
        self._kill_timer.start(int(self.shutdown_kill_seconds * 1000))

    def _on_kill_timeout(self) -> None:
        if (self._shutdown_gen != 0 and self._shutdown_gen != self._current_generation) or not self._is_shutting_down:
            log.info("忽略已废弃代次的强制终止超时 (shutdown_gen=%d != current=%d)",
                     self._shutdown_gen, self._current_generation)
            return
        target_pgid = self._shutdown_pgid if self._shutdown_pgid is not None else self._pgid
        target_proc = self._shutdown_proc if self._shutdown_proc is not None else self._proc
        proc_alive = target_proc is not None and target_proc.state() != QProcess.ProcessState.NotRunning
        pgid_alive = self._is_pgid_alive(target_pgid)
        if not proc_alive and not pgid_alive:
            self._check_and_emit_shutdown_completed()
            return
        log.warning("后端进程组在 SIGTERM 后 %s 秒仍未退出，向目标进程组 (PGID=%s) 强制发送 SIGKILL",
                    self.shutdown_kill_seconds, target_pgid)
        self._send_signal_to_target_pgid(target_pgid, target_proc, signal.SIGKILL)
        self._cleanup_specific_pgid(target_pgid, synchronous=False)
        self._check_and_emit_shutdown_completed()

    def _send_signal_to_target_pgid(self, pgid: int | None, proc: QProcess | None, sig: signal.Signals) -> None:
        if pgid and pgid > 1:
            try:
                # 只有当 PGID 独立于当前主进程进程组时才向进程组广播
                if pgid != os.getpgrp():
                    os.killpg(pgid, sig)
                    log.info("已向目标进程组 %d 发送信号 %s", pgid, sig.name)
                elif proc and proc.state() != QProcess.ProcessState.NotRunning:
                    proc.terminate() if sig == signal.SIGTERM else proc.kill()
            except ProcessLookupError:
                pass
            except Exception as e:
                log.warning("向目标进程组 %d 发送信号 %s 失败: %s", pgid, sig.name, e)

    def _send_signal_to_group(self, sig: signal.Signals) -> None:
        self._send_signal_to_target_pgid(self._pgid, self._proc, sig)

    def _is_pgid_alive(self, pgid: int | None) -> bool:
        """检查指定进程组内是否仍有存活（非僵尸）进程。"""
        if not pgid or pgid <= 1 or pgid == os.getpgrp():
            return False
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except Exception:
            return False

        # killpg(0) 成功说明进程表中仍有记录；排查是否全部为已退出但尚未回收的僵尸进程 (Z)
        try:
            for stat_path in glob.glob("/proc/[0-9]*/stat"):
                try:
                    with open(stat_path, "r", encoding="ascii", errors="ignore") as f:
                        content = f.read()
                    idx = content.rfind(")")
                    if idx == -1:
                        continue
                    rest = content[idx + 2:].split()
                    if int(rest[2]) == pgid and rest[0] != "Z":
                        return True
                except (OSError, IndexError, ValueError):
                    continue
            return False
        except Exception:
            return True

    def _cleanup_specific_pgid(
        self,
        pgid: int | None,
        term_wait_sec: float = 0.5,
        kill_wait_sec: float = 0.5,
        synchronous: bool = False,
    ) -> None:
        """清理指定进程组内残留的孤儿进程（如 readsb），递进升级至 SIGKILL 直到确认全部退出。
        默认在后台工作线程中执行，绝不阻塞 Qt 主线程。
        """
        if not pgid or pgid <= 1 or pgid == os.getpgrp():
            if pgid:
                self._cleanup_completed_signal.emit(pgid)
            return

        self._cleaning_pgids.add(pgid)

        def _worker():
            if not self._is_pgid_alive(pgid):
                self._cleanup_completed_signal.emit(pgid)
                return

            log.info("向残留进程组 %d 发送 SIGTERM…", pgid)
            try:
                os.killpg(pgid, signal.SIGTERM)
            except ProcessLookupError:
                self._cleanup_completed_signal.emit(pgid)
                return
            except Exception as e:
                log.warning("向进程组 %d 发送 SIGTERM 异常: %s", pgid, e)

            # 等待 SIGTERM 生效
            deadline = time.monotonic() + term_wait_sec
            while time.monotonic() < deadline:
                if not self._is_pgid_alive(pgid):
                    log.info("进程组 %d 子进程已在 SIGTERM 后退出", pgid)
                    self._cleanup_completed_signal.emit(pgid)
                    return
                time.sleep(0.02)

            # 仍有存活子进程，强制升级至 SIGKILL
            log.warning("进程组 %d 在 SIGTERM 后仍有存活子进程，强制升级发送 SIGKILL…", pgid)
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                self._cleanup_completed_signal.emit(pgid)
                return
            except Exception as e:
                log.warning("向进程组 %d 发送 SIGKILL 异常: %s", pgid, e)

            # 等待 SIGKILL 生效
            deadline = time.monotonic() + kill_wait_sec
            while time.monotonic() < deadline:
                if not self._is_pgid_alive(pgid):
                    log.info("进程组 %d 残留子进程已在 SIGKILL 后退出", pgid)
                    self._cleanup_completed_signal.emit(pgid)
                    return
                time.sleep(0.02)

            log.warning("进程组 %d 在 SIGKILL 之后仍检测到存活状态", pgid)
            self._cleanup_completed_signal.emit(pgid)

        if synchronous:
            _worker()
        else:
            # 使用非 daemon 线程确保即使主线程退出循环，操作系统也会等待强杀逻辑执行完毕
            threading.Thread(target=_worker, name=f"adsb-cleanup-pgid-{pgid}", daemon=False).start()

    def _cleanup_process_group(self, synchronous: bool = False) -> None:
        """清理所属进程组内可能残留的孤儿进程（如 readsb）。"""
        self._cleanup_specific_pgid(self._pgid, synchronous=synchronous)
        if self._pgid == os.getpgrp() and self._proc and self._proc.state() != QProcess.ProcessState.NotRunning:
            try:
                self._proc.kill()
            except Exception:
                pass
            except Exception as e:
                log.debug("清理进程组 %d 时产生预期的捕获异常: %s", self._pgid, e)

    def _on_cleanup_completed(self, pgid: int) -> None:
        log.info("进程组 %d 清理完成通知已收到", pgid)
        self._cleaning_pgids.discard(pgid)
        if self._is_shutting_down:
            self._check_and_emit_shutdown_completed()
