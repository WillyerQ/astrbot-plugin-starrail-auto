"""
崩铁体力自动化管理插件

功能：自动计算体力恢复时间，通过 WOL 唤醒 Windows PC，
SSH 运行三月七助手清体力，每日自动重置。
兼容 WOL 唤醒后处于登录界面的场景，无需设置自动登录。
"""
import asyncio
import json
import os
import paramiko
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from PIL import Image, ImageDraw, ImageFont

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

# ====== 插件文件日志 ======
_plugin_dir = os.path.dirname(os.path.abspath(__file__))
_log_dir = os.path.join(_plugin_dir, "logs")
os.makedirs(_log_dir, exist_ok=True)
_file_logger = logging.getLogger("starrail_auto")
_file_logger.setLevel(logging.DEBUG)
# 清除旧 handler，避免热重载重复添加
_file_logger.handlers.clear()
_handler = logging.FileHandler(
    os.path.join(_log_dir, f"starrail_{datetime.now().strftime('%Y%m%d')}.log"),
    encoding="utf-8"
)
_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"
))
_file_logger.addHandler(_handler)
_file_logger.propagate = False

def debug_log(msg: str):
    """写 debug 日志到文件（同时输出到 AstrBot 日志）"""
    _file_logger.debug(msg)
    logger.debug(f"[starrail-auto] {msg}")

def info_log(msg: str):
    _file_logger.info(msg)
    logger.info(f"[starrail-auto] {msg}")

def warn_log(msg: str):
    _file_logger.warning(msg)
    logger.warning(f"[starrail-auto] {msg}")

def error_log(msg: str):
    _file_logger.error(msg)
    logger.error(f"[starrail-auto] {msg}")
# ======

# 时区
CST = timezone(timedelta(hours=8))

RUNNER_MODES = {"exe", "python"}
SESSION_MODES = {"console", "rdp"}


@register("starrail_auto", "AstrBot", "崩铁体力自动化管理", "1.4.1")
class StarRailAutoPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.current_stamina = None
        self.last_update_time = None
        self.trigger_time = None
        self.trigger_task = None
        # 保存 WebUI 配置（AstrBotConfig 是 dict 子类，可直接用）
        self.plugin_config = config or {}

    async def initialize(self):
        info_log("崩铁体力自动化插件已加载")

    # ========== 命令处理（@filter.command 注册，优先于 LLM） ==========

    @filter.command("体力设置")
    async def handle_set_stamina(self, event: AstrMessageEvent):
        """设置体力值：/体力设置 <数值>"""
        parts = (event.message_str or "").strip().split()
        if len(parts) < 2:
            yield event.plain_result("格式：/体力设置 <数值>，如 /体力设置 80")
            return
        try:
            stamina = int(parts[1])
            if not (0 <= stamina <= 240):
                yield event.plain_result("体力值应在 0-240 之间")
                return
        except ValueError:
            yield event.plain_result("格式：/体力设置 <数值>，如 /体力设置 80")
            return
        self.current_stamina = stamina
        self.last_update_time = datetime.now(CST)
        threshold = self._get_config("stamina_threshold", 160)
        stamina_needed = threshold - stamina
        if stamina_needed <= 0:
            yield event.plain_result(f"当前体力 {stamina}，已达到阈值 {threshold}，立即触发清体力！")
            asyncio.create_task(self._execute_cleanup_task(None))
            return
        wait_minutes = stamina_needed * 6
        self.trigger_time = self.last_update_time + timedelta(minutes=wait_minutes)
        self._schedule_trigger()
        yield event.plain_result(f"✅ 已记录！当前体力：{stamina}\n📊 距阈值 {threshold} 还差 {stamina_needed} 点\n⏱ 需要等待 {wait_minutes} 分钟（{wait_minutes//60}小时{wait_minutes%60}分钟）\n🔔 预计触发时间：{self.trigger_time.strftime(chr(37)+chr(72)+chr(58)+chr(37)+chr(77))}")

    @filter.command("体力状态")
    async def handle_status(self, event: AstrMessageEvent):
        """查询体力状态"""
        yield event.plain_result(self._get_status_text())

    @filter.command("清体力")
    async def handle_cleanup(self, event: AstrMessageEvent):
        """手动触发清体力"""
        yield event.plain_result("正在执行清体力任务...")
        asyncio.create_task(self._execute_cleanup_task(None))

    @filter.command("体力重置")
    async def handle_reset(self, event: AstrMessageEvent):
        """重置体力数据"""
        self.current_stamina = None
        self.last_update_time = None
        self.trigger_time = None
        yield event.plain_result("体力数据已重置，请用 /体力设置 <数值> 设置初始值")

    @filter.command("体力配置")
    async def handle_config(self, event: AstrMessageEvent):
        """设置配置项：/体力配置 <key>=<value>"""
        msg = (event.message_str or "").strip()
        parts = msg.split(maxsplit=2)
        if len(parts) < 2:
            yield event.plain_result(
                "用法：/体力配置 <key>=<value>\n"
                "示例：/体力配置 pc_ip=192.168.1.100\n"
                "      /体力配置 pc_mac=AA:BB:CC:DD:EE:FF\n"
                "      /体力配置 wol_method=ssh\n"
                "当前配置保存在插件目录 config.json"
            )
            return
        pair = parts[1]
        if "=" not in pair:
            yield event.plain_result("格式错误，请用 key=value 格式")
            return
        key, value = pair.split("=", 1)
        self._save_local_config({key.strip(): value.strip()})
        yield event.plain_result(f"✅ 已保存: {key.strip()} = {value.strip()}")

    @filter.command("体力重置配置")
    async def handle_reset_config(self, event: AstrMessageEvent):
        """删除本地配置"""
        path = self._config_path()
        if os.path.exists(path):
            os.remove(path)
            yield event.plain_result("✅ 本地配置已删除，请重新设置")
        else:
            yield event.plain_result("ℹ️ 本地配置不存在")

    @filter.command("体力源码检查")
    async def handle_source_check(self, event: AstrMessageEvent):
        """检查 Windows 端三月七源码运行环境"""
        errors = self._validate_pc_ssh_config()
        if errors:
            yield event.plain_result("⚠️ 配置不完整：\n" + "\n".join(f"- {e}" for e in errors))
            return

        repo = self._get_config("m7a_repo_path", "")
        if not repo:
            yield event.plain_result("⚠️ 请先配置 m7a_repo_path，例如 /体力配置 m7a_repo_path=C:\\Users\\he\\M7A")
            return

        venv_python = self._get_config("python_path", rf"{repo}\.venv\Scripts\python.exe")
        cmd = (
            f'cd /d "{repo}" && '
            f'echo [repo] %cd% && '
            f'if exist app.py (echo [ok] app.py) else (echo [miss] app.py) && '
            f'if exist main.py (echo [ok] main.py) else (echo [miss] main.py) && '
            f'if exist "{venv_python}" ("{venv_python}" --version && '
            f'"{venv_python}" -c "import pyuac; print(\'[ok] pyuac\')" 2^>nul || echo [miss] pyuac) '
            f'else echo [miss] venv python: {venv_python}'
        )
        output = await asyncio.to_thread(self._pc_ssh_exec, cmd, 60)
        yield event.plain_result("📋 三月七源码环境检查：\n" + output[-3000:])

    @filter.command("体力源码初始化")
    async def handle_source_bootstrap(self, event: AstrMessageEvent):
        """在 Windows 端初始化三月七源码 venv（避免 uv run 使用 3.13）"""
        errors = self._validate_pc_ssh_config()
        if errors:
            yield event.plain_result("⚠️ 配置不完整：\n" + "\n".join(f"- {e}" for e in errors))
            return

        repo = self._get_config("m7a_repo_path", "")
        if not repo:
            yield event.plain_result("⚠️ 请先配置 m7a_repo_path，例如 /体力配置 m7a_repo_path=C:\\Users\\he\\M7A")
            return

        venv_python = rf"{repo}\.venv\Scripts\python.exe"
        entry = self._get_config("m7a_entry", "main.py")
        yield event.plain_result("⏳ 正在通过 SSH 初始化三月七源码环境，请稍等几分钟...")

        ps_filter = (
            'powershell -NoProfile -ExecutionPolicy Bypass -Command '
            '"if (Test-Path requirements.txt) { '
            "Get-Content requirements.txt | Where-Object { $_ -notmatch '^\\s*opencc([=<>!~ ]|$)' } "
            '| Set-Content requirements.starrail_auto.txt -Encoding UTF8 }"'
        )
        cmd = " && ".join([
            f'cd /d "{repo}"',
            'uv venv -p 3.12 .venv',
            f'"{venv_python}" -m pip install -U pip setuptools wheel',
            ps_filter,
            f'uv pip install pyuac opencc-python-reimplemented --python "{venv_python}"',
            f'if exist requirements.starrail_auto.txt (uv pip install -r requirements.starrail_auto.txt --python "{venv_python}") else (echo [warn] requirements.txt not found, skipped)',
            f'"{venv_python}" --version',
            f'"{venv_python}" -c "import pyuac; import opencc; print(\'BOOTSTRAP_OK\')"',
        ])
        output = await asyncio.to_thread(self._pc_ssh_exec, cmd, 1800)

        if "BOOTSTRAP_OK" in output:
            self._save_local_config({
                "runner_mode": "python",
                "python_path": venv_python,
                "m7a_repo_path": repo,
                "m7a_entry": entry,
                "gui_session_mode": "rdp",
            })
            yield event.plain_result(
                "✅ 三月七源码环境初始化完成，已自动写入插件配置：\n"
                f"runner_mode=python\npython_path={venv_python}\n"
                f"m7a_repo_path={repo}\nm7a_entry={entry}\ngui_session_mode=rdp"
            )
        else:
            yield event.plain_result("❌ 初始化可能失败，最后输出：\n" + output[-3000:])

    @filter.command("体力帮助")
    async def handle_help(self, event: AstrMessageEvent):
        """显示帮助"""
        img_path = await self._generate_help_image()
        if img_path and os.path.exists(img_path):
            yield event.image_result(img_path)
        else:
            yield event.plain_result(self._get_help_text())

    async def on_llm_request(self, event: AstrMessageEvent):
        """自然语言触发——仅处理不含 / 前缀的消息"""
        msg = (event.message_str or "").strip()
        if msg.startswith("/"):
            return
        if any(kw in msg for kw in ["崩铁日常", "跑崩铁", "清体力啦"]):
            if self.current_stamina is None:
                yield event.plain_result("还没设置体力，请先告诉体力值（或 /体力设置 <数值>）")
            else:
                yield event.plain_result(f"当前体力 {self.current_stamina}，正在执行...")
                asyncio.create_task(self._execute_cleanup_task(None))

    # ========== 体力设置逻辑 ==========

    def _handle_set_stamina(self, message_str: str, event):
        """解析并设置体力值"""
        parts = message_str.split()
        if len(parts) < 2:
            yield event.plain_result("格式：/体力设置 <数值>，如 /体力设置 80")
            return

        try:
            stamina = int(parts[1])
            if not (0 <= stamina <= 240):
                yield event.plain_result("体力值应在 0-240 之间")
                return
        except ValueError:
            yield event.plain_result("格式：/体力设置 <数值>，如 /体力设置 80")
            return

        self.current_stamina = stamina
        self.last_update_time = datetime.now(CST)

        threshold = self._get_config("stamina_threshold", 160)
        stamina_needed = threshold - stamina

        if stamina_needed <= 0:
            yield event.plain_result(f"当前体力 {stamina}，已达到阈值 {threshold}，立即触发清体力！")
            asyncio.create_task(self._execute_cleanup_task(None))
            return

        wait_minutes = stamina_needed * 6
        self.trigger_time = self.last_update_time + timedelta(minutes=wait_minutes)
        self._schedule_trigger()

        yield event.plain_result(
            f"✅ 已记录！当前体力：{stamina}\n"
            f"📊 距阈值 {threshold} 还差 {stamina_needed} 点\n"
            f"⏱ 需要等待 {wait_minutes} 分钟（{wait_minutes//60}小时{wait_minutes%60}分钟）\n"
            f"🔔 预计触发时间：{self.trigger_time.strftime('%H:%M')}"
        )

    def _get_status_text(self) -> str:
        if self.current_stamina is None:
            return "ℹ️ 尚未设置体力，请用 /体力设置 <数值> 初始化"

        threshold = self._get_config("stamina_threshold", 160)
        stamina_needed = threshold - self.current_stamina
        now = datetime.now(CST)

        if self.last_update_time and stamina_needed > 0:
            elapsed_minutes = (now - self.last_update_time).total_seconds() / 60
            current_est = min(threshold, self.current_stamina + int(elapsed_minutes / 6))
        else:
            current_est = self.current_stamina

        text = (
            f"📊 **崩铁体力状态**\n"
            f"当前体力（记录）：{self.current_stamina}\n"
            f"当前体力（估算）：{current_est}\n"
            f"阈值：{threshold}\n"
        )

        if stamina_needed > 0 and self.last_update_time:
            remaining = (stamina_needed * 60) - (now - self.last_update_time).total_seconds() / 60
            if remaining > 0:
                text += f"距下次触发：约 {int(remaining)} 分钟\n"
            else:
                text += "⏰ 已达到阈值时间，等待自动触发\n"

        if self.trigger_time:
            text += f"计划触发：{self.trigger_time.strftime('%H:%M')}"

        return text

    # ========== 执行清体力（WOL + SSH + 计划任务） ==========

    @staticmethod
    def _escape_win_cmd_arg(s: str) -> str:
        """转义 Windows cmd.exe 参数中的特殊字符（防注入）"""
        # schtasks /rp 用双引号包裹，内部的双引号需转义为 ""
        # cmd.exe 中 % 要加倍为 %% 才不会展开
        return s.replace("%", "%%").replace('"', '""')

    async def _execute_cleanup(self, event=None):
        """执行清体力任务：WOL → SSH → 更新检查 → 跑任务 → 报错日志"""
        pc_ip = self._get_config("pc_ip", "")
        pc_mac = self._get_config("pc_mac", "")
        pc_username = self._get_config("pc_username", "")
        pc_password = self._get_config("pc_password", "")
        march7th_path = self._get_config("march7th_path", "")
        runner_mode = self._get_config("runner_mode", "exe")
        python_path = self._get_config("python_path", "python")
        m7a_repo_path = self._get_config("m7a_repo_path", "")
        m7a_entry = self._get_config("m7a_entry", "app.py")
        gui_runner_path = self._get_config("gui_runner_path", r"C:\Users\Public\starrail_auto_run.cmd")
        gui_session_mode = self._get_config("gui_session_mode", "console")
        ssh_port = self._get_config("ssh_port", 22)

        admin_id = None
        if event:
            try:
                admin_id = event.get_sender_id()
            except Exception:
                admin_id = None

        config_errors = self._validate_runtime_config()
        if config_errors:
            msg = "⚠️ 配置不完整：\n" + "\n".join(f"- {e}" for e in config_errors)
            info_log(msg)
            if event:
                yield event.plain_result(msg)
            return

        # 0. 快速探测：PC 是否已开机？
        pc_already_on = False
        try:
            probe = paramiko.SSHClient()
            probe.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            probe.connect(hostname=pc_ip, port=ssh_port,
                          username=pc_username, password=pc_password, timeout=5)
            probe.close()
            pc_already_on = True
            info_log(f"✅ PC {pc_ip} 已开机，跳过 WOL 唤醒")
        except Exception:
            info_log(f"📡 PC {pc_ip} 未响应，需要 WOL 唤醒")

        # 1. WOL 唤醒
        if pc_already_on:
            info_log("⏭️ PC 已开机，跳过 WOL 唤醒阶段")
        else:
            if event: info_log("📡 发送 WOL 唤醒信号...")
            broadcast_ip = self._get_config("broadcast_ip", "")
            if not broadcast_ip and pc_ip:
                parts = pc_ip.rsplit(".", 1)
                if len(parts) == 2:
                    broadcast_ip = f"{parts[0]}.255"
            if not broadcast_ip:
                broadcast_ip = "192.168.1.255"
            wol_method = self._get_config("wol_method", "ssh")
            nas_user = self._get_config("nas_ssh_user", "root")
            nas_host = self._get_config("nas_ssh_host", "127.0.0.1")
            nas_port = self._get_config("nas_ssh_port", 22)
            nas_password = self._get_config("nas_ssh_password", "")
            await self._send_wol(pc_mac, broadcast_ip, wol_method, nas_user,
                                 nas_host, nas_port, nas_password)
            # 给电脑 90 秒开机进系统（部分 PC 需要更长时间）
            await asyncio.sleep(90)

        # 2. SSH 连接（带重试）
        if event: info_log("🔗 正在通过 SSH 连接电脑...")
        ssh = None
        ssh_connected = False
        for attempt in range(1, 4):
            try:
                ssh = paramiko.SSHClient()
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                ssh.connect(hostname=pc_ip, port=ssh_port,
                            username=pc_username, password=pc_password, timeout=20)
                ssh_connected = True
                break
            except Exception as e:
                if attempt < 3:
                    wait = attempt * 30
                    info_log(f"⏳ SSH 连接失败（第{attempt}次），{wait}秒后重试... ({e})")
                    await asyncio.sleep(wait)
                else:
                    raise

        if not ssh_connected:
            raise Exception(f"SSH 连接失败（已重试3次）")

        try:
            has_session, session_text = self._has_supported_desktop_session(ssh, gui_session_mode)
            if not has_session:
                msg = (
                    "❌ 当前 Windows 没有可用的已登录桌面会话，GUI 游戏无法启动。"
                    "console 模式请手动登录/锁屏；rdp 模式请先 RDP 登录一次并保持会话不断开注销。"
                )
                info_log(msg)
                if session_text:
                    debug_log("Windows 会话状态:\n" + session_text)
                if event:
                    yield event.plain_result(msg)
                ssh.close()
                return

            selected_tasks = self._normalize_tasks(self._get_config("selected_tasks", ["main"]))
            task_arg_str = " ".join(selected_tasks)
            task_names = {
                "main": "完整运行", "daily": "每日实训", "weekly": "周常",
                "universe_gui": "模拟宇宙", "forgottenhall": "忘却之庭",
                "echo_of_war": "历战余响", "assignment": "委托", "quest": "任务"
            }
            if event:
                labels = [task_names.get(t, t) for t in selected_tasks]
                info_log(f"⚙️ 即将执行：{' → '.join(labels)}")

            if runner_mode == "python":
                march7th_dir = m7a_repo_path.rstrip("\\/")
                runner_script = self._build_runner_script(
                    runner_mode=runner_mode,
                    workdir=march7th_dir,
                    runner_path=python_path,
                    entry_path=m7a_entry,
                    task_args=task_arg_str,
                )
            else:
                march7th_dir = march7th_path.rsplit("\\", 1)[0] if "\\" in march7th_path else march7th_path.rsplit("/", 1)[0]
                updater_path = march7th_dir + "\\March7th Updater.exe"

                # 3. 检查更新器是否存在（不运行 GUI 程序，锁屏下会卡住）
                if event:
                    info_log("🔍 检查更新器是否存在...")
                stdin, stdout, stderr = ssh.exec_command(
                    f'if exist "{updater_path}" (echo EXISTS) else (echo NOT_FOUND)',
                    timeout=10
                )
                try:
                    stdout.channel.recv_exit_status(timeout=10)
                    check_result = stdout.read().decode("utf-8", errors="ignore").strip()
                except Exception:
                    check_result = "TIMEOUT"
                    stdout.channel.close()
                    stderr.channel.close()

                if check_result == "EXISTS":
                    if event:
                        info_log("ℹ️ 更新器存在（锁屏下跳过运行，在主任务中自动更新）")
                else:
                    if event:
                        info_log("ℹ️ 未找到独立更新器，跳过")

                runner_script = self._build_runner_script(
                    runner_mode=runner_mode,
                    workdir=march7th_dir,
                    runner_path=march7th_path,
                    entry_path=m7a_entry,
                    task_args=task_arg_str,
                )

                # 3. 检查更新器是否存在（不运行 GUI 程序，锁屏下会卡住）
                if event:
                    info_log("🔍 检查更新器是否存在...")
                stdin, stdout, stderr = ssh.exec_command(
                    f'if exist "{updater_path}" (echo EXISTS) else (echo NOT_FOUND)',
                    timeout=10
                )
                try:
                    stdout.channel.recv_exit_status(timeout=10)
                    check_result = stdout.read().decode("utf-8", errors="ignore").strip()
                except Exception:
                    check_result = "TIMEOUT"
                    stdout.channel.close()
                    stderr.channel.close()

                if check_result == "EXISTS":
                    if event:
                        info_log("ℹ️ 更新器存在（锁屏下跳过运行，在主任务中自动更新）")
                else:
                    if event:
                        info_log("ℹ️ 未找到独立更新器，跳过")

                runner_script = self._build_runner_script(
                    runner_mode=runner_mode,
                    workdir=march7th_dir,
                    runner_path=march7th_path,
                    entry_path=m7a_entry,
                    task_args=task_arg_str,
                )

            # 5. 写入临时启动脚本
            schtasks_name = "StarRailAutoTemp"
            self._write_remote_text_file(ssh, gui_runner_path, runner_script)
            safe_runner_path = self._escape_win_cmd_arg(gui_runner_path)
            safe_password = self._escape_win_cmd_arg(pc_password)
            safe_username = self._escape_win_cmd_arg(pc_username)

            # 两步法：先创建交互式任务(/it)，再绑定用户(/change)
            # 这样 GUI 程序才能在用户桌面显示
            cmds = " & ".join([
                f'schtasks /delete /tn "{schtasks_name}" /f 2>nul',
                f'schtasks /create /tn "{schtasks_name}" /tr "cmd.exe /c call ""{safe_runner_path}""" '
                f'/sc once /st 23:59 /it /f',
                f'schtasks /change /tn "{schtasks_name}" '
                f'/ru "{safe_username}" /rp "{safe_password}"',
                f'schtasks /run /tn "{schtasks_name}"',
            ])
            stdin, stdout, stderr = ssh.exec_command(cmds, timeout=30)
            raw_out = stdout.read()
            raw_err = stderr.read()[:500]
            ssh.close()

            # Windows 输出是 GBK 编码
            try:
                result = raw_out.decode("gbk", errors="ignore")
                err_text = raw_err.decode("gbk", errors="ignore")
            except Exception:
                result = raw_out.decode("utf-8", errors="ignore")
                err_text = raw_err.decode("utf-8", errors="ignore")

            if "成功" in result:
                info_log("✅ 计划任务创建成功, 正在运行...")
            else:
                info_log(f"❌ 计划任务失败: {err_text or result[:200]}")
                info_log("💡 可能原因：用户名错误, 权限不足")
                if event:
                    yield event.plain_result("❌ 计划任务创建失败, 请检查配置")
                return

            # 6. 轮询等待完成
            info_log("⏳ 等待任务完成...")
            task_done = False
            for _ in range(30):
                await asyncio.sleep(60)
                try:
                    cs = paramiko.SSHClient()
                    cs.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                    cs.connect(hostname=pc_ip, port=ssh_port,
                               username=pc_username, password=pc_password, timeout=10)
                    _, out, _ = cs.exec_command(
                        f'schtasks /query /tn "{schtasks_name}" /fo LIST | find "状态:"', timeout=10)
                    if "准备就绪" in out.read().decode("utf-8", errors="ignore"):
                        task_done = True
                    cs.close()
                    if task_done:
                        break
                except Exception:
                    pass

            # 7. 清理计划任务
            try:
                cs2 = paramiko.SSHClient()
                cs2.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                cs2.connect(hostname=pc_ip, port=ssh_port,
                            username=pc_username, password=pc_password, timeout=10)
                cs2.exec_command(f'schtasks /delete /tn "{schtasks_name}" /f', timeout=10)
                cs2.close()
            except Exception:
                pass

            # 8. 拉取日志
            log_content = ""
            try:
                ls = paramiko.SSHClient()
                ls.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                ls.connect(hostname=pc_ip, port=ssh_port,
                           username=pc_username, password=pc_password, timeout=10)
                # 查找最新日志文件
                find_cmd = (
                    f'for /f "delims=" %f in (\'dir /b /o-d "{march7th_dir}\\logs\\*.log" "{march7th_dir}\\logs\\*.txt" '
                    f'"{march7th_dir}\\*.log" 2^>nul\') do @echo %f && exit /b'
                )
                _, out, _ = ls.exec_command(find_cmd, timeout=10)
                latest_log = out.read().decode("utf-8", errors="ignore").strip().split(chr(10))[0].strip()

                if latest_log and latest_log.endswith((".log", ".txt")):
                    log_path = f"{march7th_dir}\\logs\\{latest_log}" if "\\" not in latest_log else latest_log
                    _, log_out, log_err = ls.exec_command(f'type "{log_path}" 2>nul', timeout=10)
                    log_content = log_out.read().decode("utf-8", errors="ignore")
                    log_stderr = log_err.read().decode("utf-8", errors="ignore")
                    if log_stderr:
                        log_content += "\n" + log_stderr
                ls.close()
            except Exception:
                pass

            # 9. 结果
            if task_done:
                info_log("✅ 三月七助手任务已完成！")
            else:
                error_md = "## ❌ 三月七助手执行异常\n\n"
                error_md += "**状态：** 任务超时或未正常完成\n\n"
                if log_content:
                    error_md += "**错误日志：**\n\n```\n" + log_content[-2000:] + "\n```\n"
                else:
                    error_md += "**错误日志：** 未找到日志文件\n"
                error_md += "\n---\n*由 starrail-auto 插件自动报告*"

                info_log("⏰ 任务可能异常，正在发送报错日志...")
                info_log(error_md)

            # 10. 自动关机
            if self._get_config("auto_shutdown", True):
                info_log("🔌 电脑即将关机")
                try:
                    cs3 = paramiko.SSHClient()
                    cs3.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                    cs3.connect(hostname=pc_ip, port=ssh_port,
                                username=pc_username, password=pc_password, timeout=10)
                    cs3.exec_command("shutdown /s /t 30")
                    cs3.close()
                except Exception:
                    pass

        except Exception as e:
            err_msg = f"❌ 操作失败：{str(e)}"
            if event: info_log(err_msg)
            # 发送详细报错
            import traceback
            tb = traceback.format_exc()
            error_md = "## ❌ 插件执行崩溃\n\n"
            error_md += f"**异常类型：** `{type(e).__name__}`\n\n"
            error_md += "**错误信息：**\n```\n" + str(e) + "\n```\n\n"
            error_md += "**调用栈：**\n```\n" + tb[-1500:] + "\n```\n"
            error_md += "\n---\n*由 starrail-auto 插件自动报告*"
            info_log(error_md)
            error_log(f"插件执行崩溃: {e}\
{tb}")

    async def _execute_cleanup_task(self, event=None):
        """消耗 _execute_cleanup 生成器, 供 create_task 使用"""
        async for _ in self._execute_cleanup(event):
            pass

    # ========== 定时任务 ==========

    def _schedule_trigger(self):
        if not self.trigger_time:
            return
        now = datetime.now(CST)
        delay = (self.trigger_time - now).total_seconds()
        if delay <= 0:
            asyncio.create_task(self._execute_cleanup_task(None))
            return
        if self.trigger_task and not self.trigger_task.done():
            self.trigger_task.cancel()
        async def delayed():
            await asyncio.sleep(delay)
            info_log("定时触发：执行清体力任务")
            async for _ in self._execute_cleanup(None): pass
        self.trigger_task = asyncio.create_task(delayed())
        info_log(f"定时任务已设置，{delay/60:.1f} 分钟后触发")

    # ========== 工具方法 ==========

    def _config_path(self) -> str:
        """本地配置文件路径"""
        return os.path.join(_plugin_dir, "config.json")

    def _load_local_config(self) -> dict:
        """从本地 JSON 文件加载配置"""
        path = self._config_path()
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                warn_log(f"配置损坏，重置: {e}")
                os.remove(path)
        return {}

    def _save_local_config(self, cfg: dict):
        """保存配置到本地 JSON 文件"""
        path = self._config_path()
        try:
            # 合并现有配置
            existing = self._load_local_config()
            existing.update(cfg)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)
            info_log(f"配置已保存到 {path}")
        except Exception as e:
            error_log(f"保存配置失败: {e}")

    def _get_config(self, key: str, default=None):
        """获取配置：优先从本地 JSON，其次从 WebUI 配置"""
        # 1. 本地 JSON 文件（/体力配置 命令写入的）优先
        try:
            local = self._load_local_config()
            if key in local and local[key] not in (None, "", [], {}):
                return local[key]
        except Exception:
            pass
        # 2. WebUI 插件配置页面填写
        try:
            if hasattr(self, "plugin_config"):
                val = self.plugin_config.get(key)
                if val is not None:
                    return val
        except Exception:
            pass
        # 3. 从 AstrBot 全局配置兜底
        try:
            conf = self.context.get_config()
            val = conf.get(key) if hasattr(conf, "get") else None
            if val is not None and not hasattr(val, "get"):
                return val
        except Exception:
            pass
        return default

    def _validate_pc_ssh_config(self) -> list[str]:
        """检查远程 Windows SSH 所需配置"""
        errors = []
        required = {
            "pc_ip": "电脑 IP（pc_ip）",
            "pc_username": "Windows 用户名（pc_username）",
            "pc_password": "Windows 密码（pc_password）",
        }
        for key, label in required.items():
            if not self._get_config(key, ""):
                errors.append(f"缺少 {label}")
        return errors

    def _pc_ssh_exec(self, command: str, timeout: int = 120) -> str:
        """在 Windows PC 上执行命令并返回 stdout/stderr"""
        pc_ip = self._get_config("pc_ip", "")
        pc_username = self._get_config("pc_username", "")
        pc_password = self._get_config("pc_password", "")
        ssh_port = self._get_config("ssh_port", 22)

        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            ssh.connect(hostname=pc_ip, port=ssh_port,
                        username=pc_username, password=pc_password, timeout=20)
            _, stdout, stderr = ssh.exec_command(command, timeout=timeout)
            raw_out = stdout.read()
            raw_err = stderr.read()
            try:
                out = raw_out.decode("gbk", errors="ignore")
                err = raw_err.decode("gbk", errors="ignore")
            except Exception:
                out = raw_out.decode("utf-8", errors="ignore")
                err = raw_err.decode("utf-8", errors="ignore")
            return (out + ("\n" + err if err else "")).strip()
        finally:
            try:
                ssh.close()
            except Exception:
                pass

    def _normalize_tasks(self, value) -> list[str]:
        """规范并过滤三月七助手任务列表"""
        if isinstance(value, str):
            value = [x.strip() for x in value.replace("，", ",").split(",") if x.strip()]
        if not isinstance(value, list):
            value = ["main"]
        tasks = []
        for item in value:
            item = str(item).strip()
            if item in {"main", "daily", "weekly", "universe_gui", "forgottenhall", "echo_of_war", "assignment", "quest"} and item not in tasks:
                tasks.append(item)
        return tasks or ["main"]

    def _validate_runtime_config(self) -> list[str]:
        """检查执行所需配置"""
        errors = []
        required = {
            "pc_ip": "电脑 IP（pc_ip）",
            "pc_mac": "电脑 MAC（pc_mac）",
            "pc_username": "Windows 用户名（pc_username）",
            "pc_password": "Windows 密码（pc_password）",
        }
        for key, label in required.items():
            if not self._get_config(key, ""):
                errors.append(f"缺少 {label}")

        runner_mode = self._get_config("runner_mode", "exe")
        if runner_mode not in RUNNER_MODES:
            errors.append("runner_mode 只能是 exe 或 python")
        if runner_mode == "exe":
            if not self._get_config("march7th_path", ""):
                errors.append("runner_mode=exe 时必须填写 march7th_path")
        elif runner_mode == "python":
            if not self._get_config("m7a_repo_path", ""):
                errors.append("runner_mode=python 时必须填写 m7a_repo_path")
            if not self._get_config("python_path", "python"):
                errors.append("runner_mode=python 时必须填写 python_path")

        gui_session_mode = self._get_config("gui_session_mode", "console")
        if gui_session_mode not in SESSION_MODES:
            errors.append("gui_session_mode 只能是 console 或 rdp")
        return errors

    @staticmethod
    def _escape_batch_value(value: str) -> str:
        """转义批处理中的特殊字符"""
        return str(value).replace("^", "^^").replace("%", "%%").replace("&", "^&")

    @classmethod
    def _build_runner_script(cls, runner_mode: str, workdir: str, runner_path: str,
                             entry_path: str, task_args: str) -> str:
        """生成远程临时启动脚本"""
        safe_workdir = cls._escape_batch_value(workdir)
        safe_runner_path = cls._escape_batch_value(runner_path)
        safe_entry = cls._escape_batch_value(entry_path) if entry_path else ""
        safe_task_args = cls._escape_batch_value(task_args)

        if runner_mode == "python":
            return (
                "@echo off\r\n"
                "setlocal\r\n"
                f'cd /d "{safe_workdir}"\r\n'
                f'start "StarRailAuto" /wait "{safe_runner_path}" "{safe_entry}" {safe_task_args} --exit\r\n'
                "exit /b %ERRORLEVEL%\r\n"
            )

        return (
            "@echo off\r\n"
            "setlocal\r\n"
            f'cd /d "{safe_workdir}"\r\n'
            f'start "StarRailAuto" /wait "{safe_runner_path}" {safe_task_args} --exit\r\n'
            "exit /b %ERRORLEVEL%\r\n"
        )

    @staticmethod
    def _write_remote_text_file(ssh: paramiko.SSHClient, remote_path: str, content: str):
        """通过 SFTP 写入远程文件"""
        sftp = ssh.open_sftp()
        try:
            with sftp.file(remote_path, "w") as f:
                f.write(content)
        finally:
            sftp.close()

    @classmethod
    def _has_supported_desktop_session(cls, ssh: paramiko.SSHClient, mode: str = "console") -> tuple[bool, str]:
        """检测是否存在可用的已登录桌面会话。"""
        def _run(cmd: str) -> str:
            _, stdout, _ = ssh.exec_command(cmd, timeout=10)
            return stdout.read().decode("gbk", errors="ignore")

        try:
            text = (_run("query user") + "\n" + _run("qwinsta")).strip()
            normalized = text.lower()
            console_tokens = (" active", "console", " 运行中", " 活动")
            rdp_tokens = console_tokens + ("rdp-tcp", " disc", " disconnected", " 已断开", " 断开")
            tokens = rdp_tokens if mode == "rdp" else console_tokens
            return any(token in normalized for token in tokens), text
        except Exception as e:
            return False, f"检测桌面会话失败: {e}"

    @staticmethod
    async def _send_wol(mac: str, broadcast_ip: str = "",
                        method: str = "ssh", nas_user: str = "root",
                        nas_host: str = "127.0.0.1", nas_port: int = 22,
                        nas_password: str = ""):
        """发送 WOL 唤醒信号

        策略（按优先级）：
        1. method=ssh — SSH 到同一子网的机器（NAS/宿主机）执行 etherwake
        2. method=udp — 容器内直发 etherwake / wakeonlan / 原始 UDP
        """
        mac_clean = mac.replace(":", "").replace("-", "").replace(" ", "")
        if len(mac_clean) != 12:
            error_log(f"无效 MAC: {mac}")
            return

        # === SSH 转发（推荐：适合跨子网场景） ===
        if method == "ssh":
            if not nas_host or nas_host == "127.0.0.1":
                error_log("SSH WOL 失败: 未配置 NAS_SSH_HOST（需填同一子网的机器 IP）")
                return
            # etherwake 不需要 -i 指定网段，自动广播到本地子网
            cmd = f"wakeonlan -i {broadcast_ip} {mac} 2>/dev/null || etherwake {mac_clean}"
            try:
                ssh = paramiko.SSHClient()
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                pwd = nas_password if nas_password else None
                ssh.connect(nas_host, port=nas_port, username=nas_user,
                            password=pwd, timeout=5)
                # 先尝试安装 etherwake（如果缺失）
                _, _, _ = ssh.exec_command(
                    "which wakeonlan >/dev/null 2>&1 || "
                    f"(apt-get update -qq && apt-get install -y -qq wakeonlan etherwake 2>&1)",
                    timeout=30
                )
                stdin, stdout, stderr = ssh.exec_command(cmd, timeout=10)
                result = (stdout.read().decode() + stderr.read().decode()).strip()
                ssh.close()
                info_log(f"SSH WOL 已发送至 {mac} -> 通过 {nas_host}: {result or 'OK'}")
            except Exception as e:
                error_log(f"SSH WOL 失败: {e}")
            return

        # === 直发模式（仅同子网有效） ===
        import shutil
        etherwake_path = shutil.which("etherwake")
        if etherwake_path:
            import subprocess
            try:
                subprocess.run(
                    [etherwake_path, "-i", broadcast_ip, mac_clean],
                    capture_output=True, timeout=5,
                )
                info_log(f"etherwake WOL 已发送至 {mac} -> {broadcast_ip}")
                return
            except Exception as e:
                info_log(f"etherwake 失败，尝试 wakeonlan: {e}")

        wakeonlan_path = shutil.which("wakeonlan")
        if wakeonlan_path:
            import subprocess
            try:
                subprocess.run(
                    [wakeonlan_path, "-i", broadcast_ip, mac],
                    capture_output=True, timeout=5,
                )
                info_log(f"wakeonlan WOL 已发送至 {mac} -> {broadcast_ip}")
                return
            except Exception as e:
                info_log(f"wakeonlan 失败，回退原始 UDP: {e}")

        # 最终回退：原始 UDP 魔术包
        magic = bytes.fromhex("FF" * 6 + mac_clean * 16)
        try:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.settimeout(2)
            s.sendto(magic, (broadcast_ip, 9))
            s.sendto(magic, (broadcast_ip, 7))
            s.close()
            info_log(f"原始 UDP WOL 已发送至 {mac} -> {broadcast_ip}:9/7")
        except Exception as e:
            error_log(f"原始 UDP WOL 失败: {e}")

    def _get_help_text(self) -> str:
        return ("📋 **崩铁体力自动化 - 指令列表**\n\n"
                "/体力设置 <数值>  - 设置当前体力并计算触发时间\n"
                "/体力状态         - 查看体力记录和下次触发时间\n"
                "/清体力           - 手动唤醒电脑执行清体力任务\n"
                "/体力重置         - 清除所有体力数据重新开始\n"
                "/体力帮助         - 显示本帮助\n\n"
                "💡 也可以对我说「跑崩铁」「清体力啦」来触发")

    async def _generate_help_image(self) -> Optional[str]:
        """生成帮助图片，支持自定义背景"""
        try:
            plugin_dir = os.path.dirname(os.path.abspath(__file__))
            bg_dir = os.path.join(plugin_dir, "backgrounds")

            font_path = None
            for fp in [
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
                "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
                "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            ]:
                if os.path.exists(fp):
                    font_path = fp
                    break
            if not font_path:
                import subprocess
                r = subprocess.run(["fc-list", ":lang=zh", "-f", "%{file}\n"],
                                   capture_output=True, text=True, timeout=5)
                fonts = [f.strip() for f in r.stdout.strip().split("\n") if f.strip()]
                if fonts:
                    font_path = fonts[0]
            if not font_path:
                return None

            bg_image = None
            if os.path.isdir(bg_dir):
                for f in sorted(os.listdir(bg_dir)):
                    if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                        bg_image = os.path.join(bg_dir, f)
                        break

            W, H = 600, 420
            if bg_image:
                bg = Image.open(bg_image).convert("RGB")
                bg = bg.resize((W, H), Image.LANCZOS)
                img = bg
            else:
                img = Image.new("RGB", (W, H), (25, 25, 35))

            draw = ImageDraw.Draw(img)
            tf = ImageFont.truetype(font_path, 28)
            sf = ImageFont.truetype(font_path, 16)
            cf = ImageFont.truetype(font_path, 18)
            nf = ImageFont.truetype(font_path, 14)

            if bg_image:
                overlay = Image.new("RGBA", (W, H), (0, 0, 0, 140))
                img.paste(overlay, (0, 0), overlay)

            draw.text((30, 25), "🌀 崩铁体力自动化", fill=(255, 200, 100), font=tf)
            draw.text((30, 60), "指令列表", fill=(180, 180, 200), font=sf)
            draw.rectangle([(30, 85), (570, 87)], fill=(80, 80, 100))

            cmds = [
                ("/体力设置 <数值>", " 设置当前体力，自动计算到阈值的时间"),
                ("/体力状态",         " 查看体力记录及下次触发时间"),
                ("/清体力",           " 手动唤醒电脑执行清体力任务"),
                ("/体力重置",         " 清除所有体力数据重新开始"),
                ("/体力帮助",         " 显示本帮助"),
            ]
            y = 110
            for c, d in cmds:
                draw.text((35, y), c, fill=(100, 200, 255), font=cf)
                draw.text((35, y + 24), d, fill=(180, 180, 200), font=nf)
                y += 55

            draw.rectangle([(30, y + 5), (570, y + 45)], fill=(40, 45, 60))
            draw.text((40, y + 12), "💡 也可以对我说「跑崩铁」「清体力啦」来触发",
                      fill=(255, 200, 100), font=nf)

            output_path = "/tmp/starrail_help.png"
            img.save(output_path, "PNG")
            return output_path

        except Exception as e:
            error_log(f"生成帮助图片失败: {e}")
            return None


    async def terminate(self):
        if self.trigger_task and not self.trigger_task.done():
            self.trigger_task.cancel()
            info_log("定时任务已取消")
