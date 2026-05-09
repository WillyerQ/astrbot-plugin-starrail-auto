"""
崩铁体力自动化管理插件

功能：自动计算体力恢复时间，通过 WOL 唤醒 Windows PC，
SSH 运行三月七助手清体力，每日自动重置。
兼容 WOL 唤醒后处于登录界面的场景，无需设置自动登录。
"""
import asyncio
import paramiko
from datetime import datetime, timedelta, timezone
from typing import Optional

from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

# 时区
CST = timezone(timedelta(hours=8))


@register("starrail-auto", "AstrBot", "崩铁体力自动化管理", "1.0.0")
class StarRailAutoPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.current_stamina = None
        self.last_update_time = None
        self.trigger_time = None
        self.trigger_task = None

    async def initialize(self):
        logger.info("崩铁体力自动化插件已加载")

    # ========== 命令处理（优先于 LLM） ==========

    async def on_message(self, event: AstrMessageEvent):
        """处理以 /体力 开头的插件指令，阻止 LLM 拦截"""
        message_str = event.message_str.strip()

        # 只拦截插件专属指令
        if not message_str.startswith("/体力"):
            return

        # 阻止 LLM 继续处理本条消息
        event.stop_event()

        if message_str.startswith("/体力设置"):
            for result in self._handle_set_stamina(message_str, event):
                yield result
        elif message_str == "/体力状态":
            yield event.plain_result(self._get_status_text())
        elif message_str == "/清体力":
            yield event.plain_result("正在执行清体力任务...")
            for result in self._execute_cleanup(event):
                yield result
        elif message_str == "/体力重置":
            self.current_stamina = None
            self.last_update_time = None
            self.trigger_time = None
            yield event.plain_result("体力数据已重置，请用 /体力设置 <数值> 设置初始值")
        else:
            yield event.plain_result("未知指令。可用：/体力设置、/体力状态、/清体力、/体力重置")

    async def on_llm_request(self, event: AstrMessageEvent):
        """自然语言触发——仅处理不含 / 前缀的消息"""
        msg = event.message_str.strip()

        if msg.startswith("/"):
            return

        if any(kw in msg for kw in ["崩铁日常", "跑崩铁", "清体力啦"]):
            if self.current_stamina is None:
                yield event.plain_result("还没设置体力，请先告诉我你现在的体力值（或使用 /体力设置 <数值>）")
            else:
                yield event.plain_result(f"当前体力 {self.current_stamina}，正在执行清体力...")
                for result in self._execute_cleanup(event):
                    yield result

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
            for result in self._execute_cleanup(event):
                yield result
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

    async def _execute_cleanup(self, event: Optional[AstrMessageEvent] = None):
        """执行清体力任务：WOL 唤醒 → SSH 通过计划任务执行三月七（无需自动登录）"""
        pc_ip = self._get_config("pc_ip", "")
        pc_mac = self._get_config("pc_mac", "")
        pc_username = self._get_config("pc_username", "")
        pc_password = self._get_config("pc_password", "")
        march7th_path = self._get_config("march7th_path", "")
        ssh_port = self._get_config("ssh_port", 22)

        if not pc_mac or not pc_ip:
            msg = "⚠️ 未配置电脑信息，请在 WebUI 中填写 PC_IP 和 PC_MAC"
            if event: yield event.plain_result(msg)
            logger.warning(msg)
            return

        # 1. WOL 唤醒
        if event: yield event.plain_result("📡 发送 WOL 唤醒信号...")
        await self._send_wol(pc_mac, pc_ip)

        # 等待开机（给足时间，登录界面也能 SSH）
        await asyncio.sleep(45)

        # 2. SSH 连接
        if event: yield event.plain_result("🔗 正在通过 SSH 连接电脑...")
        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(hostname=pc_ip, port=ssh_port,
                        username=pc_username, password=pc_password, timeout=20)

            # 构建任务命令
            selected_tasks = self._get_config("selected_tasks", ["main"])
            if isinstance(selected_tasks, list) and len(selected_tasks) > 0:
                task_args = " ".join(selected_tasks)
                task_cmd = f'"{march7th_path}" {task_args} --exit'
            else:
                task_cmd = f'"{march7th_path}" main --exit'

            if event:
                task_names = {
                    "main": "完整运行", "daily": "每日实训", "weekly": "周常",
                    "universe_gui": "模拟宇宙", "forgottenhall": "忘却之庭",
                    "echo_of_war": "历战余响", "assignment": "委托", "quest": "任务"
                }
                labels = [task_names.get(t, t) for t in (selected_tasks if isinstance(selected_tasks, list) else ["main"])]
                yield event.plain_result(f"⚙️ 即将执行：{' → '.join(labels)}")

            # 通过计划任务以指定用户身份运行（可在登录界面/锁屏状态下执行）
            schtasks_name = "StarRailAutoTemp"
            cmds = [
                f'schtasks /delete /tn "{schtasks_name}" /f 2>nul',
                f'schtasks /create /tn "{schtasks_name}" /tr "{task_cmd}" /sc once /st 00:00 /ru "{pc_username}" /rp "{pc_password}" /rl HIGHEST /f',
                f'schtasks /run /tn "{schtasks_name}"',
            ]
            stdin, stdout, stderr = ssh.exec_command(" && ".join(cmds), timeout=20)
            exit_code = stdout.channel.recv_exit_status()
            ssh.close()

            if exit_code == 0:
                yield event.plain_result("✅ 计划任务已触发，三月七助手正在运行...")
            else:
                err = stderr.read().decode("utf-8", errors="ignore")[:300]
                yield event.plain_result(f"⚠️ 创建任务可能异常: {err}")

            # 3. 轮询等待完成
            yield event.plain_result("⏳ 等待任务完成...")
            task_done = False
            for _ in range(30):
                await asyncio.sleep(60)
                try:
                    cs = paramiko.SSHClient()
                    cs.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                    cs.connect(hostname=pc_ip, port=ssh_port,
                               username=pc_username, password=pc_password, timeout=10)
                    _, out, _ = cs.exec_command(f'schtasks /query /tn "{schtasks_name}" /fo LIST | find "状态:"', timeout=10)
                    if "准备就绪" in out.read().decode("utf-8", errors="ignore"):
                        task_done = True
                    cs.close()
                    if task_done:
                        break
                except Exception:
                    pass

            # 清理计划任务
            try:
                cs2 = paramiko.SSHClient()
                cs2.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                cs2.connect(hostname=pc_ip, port=ssh_port,
                            username=pc_username, password=pc_password, timeout=10)
                cs2.exec_command(f'schtasks /delete /tn "{schtasks_name}" /f', timeout=10)
                cs2.close()
            except Exception:
                pass

            if task_done:
                yield event.plain_result("✅ 三月七助手任务已完成！")
            else:
                yield event.plain_result("⏰ 任务可能仍在运行，请稍后手动检查")

            # 4. 自动关机
            if self._get_config("auto_shutdown", True):
                yield event.plain_result("🔌 电脑即将关机")
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
            err = f"❌ 操作失败：{str(e)}"
            if event: yield event.plain_result(err)
            logger.error(err)

    # ========== 定时任务 ==========

    def _schedule_trigger(self):
        if not self.trigger_time:
            return
        now = datetime.now(CST)
        delay = (self.trigger_time - now).total_seconds()
        if delay <= 0:
            asyncio.create_task(self._execute_cleanup(None))
            return
        if self.trigger_task and not self.trigger_task.done():
            self.trigger_task.cancel()
        async def delayed():
            await asyncio.sleep(delay)
            logger.info("定时触发：执行清体力任务")
            await self._execute_cleanup(None)
        self.trigger_task = asyncio.create_task(delayed())
        logger.info(f"定时任务已设置，{delay/60:.1f} 分钟后触发")

    # ========== 工具方法 ==========

    def _get_config(self, key: str, default=None):
        try:
            return self.context.get_config(key) or default
        except Exception:
            return default

    @staticmethod
    async def _send_wol(mac: str, broadcast_ip: str = "192.168.1.255"):
        mac_clean = mac.replace(":", "").replace("-", "").replace(" ", "")
        if len(mac_clean) != 12:
            logger.error(f"无效 MAC: {mac}")
            return
        magic = bytes.fromhex("FF" * 6 + mac_clean * 16)
        try:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.settimeout(2)
            s.sendto(magic, (broadcast_ip, 9))
            s.sendto(magic, (broadcast_ip, 7))
            s.close()
            logger.info(f"WOL 已发送至 {mac}")
        except Exception as e:
            logger.error(f"WOL 失败: {e}")

    async def terminate(self):
        if self.trigger_task and not self.trigger_task.done():
            self.trigger_task.cancel()
            logger.info("定时任务已取消")
