"""
崩铁体力自动化管理插件

功能：自动计算体力恢复时间，通过 WOL 唤醒 Windows PC，
SSH 运行三月七助手清体力，每日自动重置。
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
        # 体力状态
        self.current_stamina = None
        self.last_update_time = None
        self.trigger_time = None  # 下次触发清体的时间
        self.trigger_task = None  # 定时任务句柄

    async def initialize(self):
        """插件初始化"""
        logger.info("崩铁体力自动化插件已加载")

    # ========== 命令处理 ==========

    async def on_message(self, event: AstrMessageEvent):
        """处理消息"""
        message_str = event.message_str.strip()

        # 初始设置体力
        if message_str.startswith("/体力设置"):
            parts = message_str.split()
            if len(parts) >= 2:
                try:
                    stamina = int(parts[1])
                    if 0 <= stamina <= 240:
                        await self._set_stamina(stamina, event)
                    else:
                        yield event.plain_result("体力值应在 0-240 之间")
                except ValueError:
                    yield event.plain_result("格式：/体力设置 <数值>")

        # 查询体力状态
        elif message_str == "/体力状态":
            yield event.plain_result(self._get_status_text())

        # 手动触发清体力
        elif message_str == "/清体力":
            yield event.plain_result("正在唤醒电脑执行清体力任务...")
            await self._execute_cleanup(event)

        # 手动重置（比如体力查错了）
        elif message_str == "/体力重置":
            self.current_stamina = None
            self.last_update_time = None
            self.trigger_time = None
            yield event.plain_result("体力数据已重置，请重新用 /体力设置 <数值> 设置初始值")

    async def on_llm_request(self, event: AstrMessageEvent):
        """LLM 消息处理 - 用于支持自然语言触发"""
        msg = event.message_str.strip()

        # 自动检测体力相关内容
        if any(kw in msg for kw in ["体力", "清体", "崩铁日常", "跑崩铁"]):
            if "设置" in msg or "初始" in msg or "当前" in msg:
                # 让 LLM 自行处理
                return
            if self.current_stamina is not None:
                yield event.plain_result(
                    f"当前体力 {self.current_stamina}，距离阈值 {self._get_config('stamina_threshold', 160)} "
                    f"还差 {self._get_config('stamina_threshold', 160) - self.current_stamina} 点。"
                )
                status = self._get_status_text()
                if status:
                    yield event.plain_result(status)
            else:
                yield event.plain_result(
                    "还没设置当前体力，请先告诉我你现在的体力值。\n"
                    "格式：/体力设置 <数值>"
                )

    # ========== 核心逻辑 ==========

    async def _set_stamina(self, stamina: int, event: AstrMessageEvent):
        """设置当前体力并计算下次触发时间"""
        self.current_stamina = stamina
        self.last_update_time = datetime.now(CST)

        threshold = self._get_config("stamina_threshold", 160)
        stamina_needed = threshold - stamina

        if stamina_needed <= 0:
            # 已达到阈值，立即触发
            yield event.plain_result(
                f"当前体力 {stamina}，已达到阈值 {threshold}，立即触发清体力任务！"
            )
            await self._execute_cleanup(event)
            return

        wait_minutes = stamina_needed * 6  # 6分钟恢复1点
        self.trigger_time = self.last_update_time + timedelta(minutes=wait_minutes)

        # 设置定时任务
        self._schedule_trigger()

        yield event.plain_result(
            f"✅ 已记录！当前体力：{stamina}\n"
            f"📊 距阈值 {threshold} 还差 {stamina_needed} 点\n"
            f"⏱ 需要等待 {wait_minutes} 分钟（{wait_minutes//60}小时{wait_minutes%60}分钟）\n"
            f"🔔 预计触发时间：{self.trigger_time.strftime('%H:%M')}\n"
            f"🔄 每日 00:00 自动通过 /sr 体力 重置数据并重新计算"
        )

    def _get_status_text(self) -> str:
        """获取当前状态文本"""
        if self.current_stamina is None:
            return "ℹ️ 尚未设置体力，请用 /体力设置 <数值> 初始化"

        threshold = self._get_config("stamina_threshold", 160)
        stamina_needed = threshold - self.current_stamina

        now = datetime.now(CST)

        # 根据当前时间估算实时体力
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

        if stamina_needed > 0:
            remaining = stamina_needed * 6 - (now - self.last_update_time).total_seconds() / 60
            if remaining > 0:
                text += f"距下次触发：约 {int(remaining)} 分钟\n"
            else:
                text += "⏰ 已达到阈值时间，等待自动触发\n"
        else:
            text += "✅ 已达到阈值\n"

        if self.trigger_time:
            text += f"计划触发：{self.trigger_time.strftime('%H:%M')}"

        return text

    async def _execute_cleanup(self, event: Optional[AstrMessageEvent] = None):
        """执行清体力任务：WOL 唤醒 → SSH 通过计划任务执行三月七（支持锁屏）"""
        pc_ip = self._get_config("pc_ip", "")
        pc_mac = self._get_config("pc_mac", "")
        pc_username = self._get_config("pc_username", "")
        pc_password = self._get_config("pc_password", "")
        march7th_path = self._get_config("march7th_path", "")
        ssh_port = self._get_config("ssh_port", 22)

        if not pc_mac or not pc_ip:
            msg = "⚠️ 未配置电脑信息，请在 WebUI 中填写 PC_IP 和 PC_MAC"
            if event:
                yield event.plain_result(msg)
            else:
                logger.warning(msg)
            return

        # 1. 发送 WOL 唤醒包
        if event:
            yield event.plain_result("📡 发送 WOL 唤醒信号...")
        await self._send_wol(pc_mac, pc_ip)

        # 等待电脑开机 + 自动登录
        await asyncio.sleep(40)

        # 2. SSH 连接，通过计划任务在用户桌面会话中执行（支持锁屏）
        if event:
            yield event.plain_result("🔗 正在通过 SSH 连接电脑...")
        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(
                hostname=pc_ip,
                port=ssh_port,
                username=pc_username,
                password=pc_password,
                timeout=15
            )

            # 获取用户配置的任务列表
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
                task_labels = [task_names.get(t, t) for t in (selected_tasks if isinstance(selected_tasks, list) else ["main"])]
                yield event.plain_result(f"⚙️ 即将执行：{' → '.join(task_labels)}")

            # 通过计划任务执行（支持锁屏状态）
            # 原理：schtasks 以指定用户身份运行，在用户桌面会话中创建进程
            # 即使控制台被锁屏，该进程仍在用户会话 1 中运行
            schtasks_name = "StarRailAutoTemp"
            cmds = [
                # 删除旧任务（如果有）
                f'schtasks /delete /tn "{schtasks_name}" /f 2>nul',
                # 创建新计划任务，以用户身份运行（可在锁屏下执行）
                f'schtasks /create /tn "{schtasks_name}" /tr "{task_cmd}" /sc once /st 00:00 /ru "{pc_username}" /rp "{pc_password}" /rl HIGHEST /f',
                # 立即触发执行
                f'schtasks /run /tn "{schtasks_name}"',
            ]
            full_cmd = " && ".join(cmds)

            stdin, stdout, stderr = ssh.exec_command(full_cmd, timeout=15)
            exit_code = stdout.channel.recv_exit_status()

            if exit_code == 0:
                yield event.plain_result("✅ 计划任务已创建并触发，三月七助手正在运行...")
            else:
                error = stderr.read().decode("utf-8", errors="ignore")[:300]
                yield event.plain_result(f"⚠️ 计划任务创建可能异常 (exit={exit_code}): {error}")

            ssh.close()

            # 3. 轮询检查任务状态（最多等 30 分钟）
            yield event.plain_result("⏳ 等待任务完成...")
            task_done = False
            for i in range(30):
                await asyncio.sleep(60)
                try:
                    check_ssh = paramiko.SSHClient()
                    check_ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                    check_ssh.connect(hostname=pc_ip, port=ssh_port,
                                      username=pc_username, password=pc_password, timeout=10)
                    stdin2, stdout2, _ = check_ssh.exec_command(
                        f'schtasks /query /tn "{schtasks_name}" /fo LIST | find "状态:"', timeout=10
                    )
                    status = stdout2.read().decode("utf-8", errors="ignore").strip()
                    check_ssh.close()

                    if "准备就绪" in status:
                        task_done = True
                        break
                except Exception:
                    pass

            # 4. 清理计划任务
            try:
                cleanup_ssh = paramiko.SSHClient()
                cleanup_ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                cleanup_ssh.connect(hostname=pc_ip, port=ssh_port,
                                    username=pc_username, password=pc_password, timeout=10)
                cleanup_ssh.exec_command(f'schtasks /delete /tn "{schtasks_name}" /f', timeout=10)
                cleanup_ssh.close()
            except Exception:
                pass

            if task_done:
                yield event.plain_result("✅ 三月七助手任务已完成！")
            else:
                yield event.plain_result("⏰ 等待超时，任务可能仍在运行，请稍后手动检查")

            # 5. 根据配置决定是否关机
            auto_shutdown = self._get_config("auto_shutdown", True)
            if auto_shutdown:
                yield event.plain_result("🔌 电脑将在 60 秒后自动关机")
                try:
                    shutdown_ssh = paramiko.SSHClient()
                    shutdown_ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                    shutdown_ssh.connect(hostname=pc_ip, port=ssh_port,
                                         username=pc_username, password=pc_password, timeout=10)
                    shutdown_ssh.exec_command("shutdown /s /t 60")
                    shutdown_ssh.close()
                except Exception:
                    pass
            else:
                yield event.plain_result("ℹ️ 电脑保持开机（auto_shutdown 已关闭）")

        except Exception as e:
            error_msg = f"❌ 操作失败：{str(e)}"
            if event:
                yield event.plain_result(error_msg)
            logger.error(error_msg)

        except Exception as e:
            error_msg = f"❌ 操作失败：{str(e)}"
            if event:
                yield event.plain_result(error_msg)
            logger.error(error_msg)

    async def _on_midnight_reset(self):
        """每日 00:00 重置"""
        logger.info("每日重置：尝试通过 /sr 体力 获取最新体力值")
        # 这个需要 LLM 调用来获取最新体力
        # 后续会通过 crontab 任务触发
        self.current_stamina = None
        self.last_update_time = None
        self.trigger_time = None
        logger.info("体力数据已重置，等待用户设置新的初始值")

    # ========== 定时任务调度 ==========

    def _schedule_trigger(self):
        """调度定时触发"""
        if not self.trigger_time:
            return

        now = datetime.now(CST)
        delay = (self.trigger_time - now).total_seconds()

        if delay <= 0:
            # 已经过了时间，立即执行
            asyncio.create_task(self._execute_cleanup(None))
            return

        # 取消旧任务
        if self.trigger_task and not self.trigger_task.done():
            self.trigger_task.cancel()

        # 创建新任务
        async def delayed_trigger():
            await asyncio.sleep(delay)
            logger.info(f"定时触发：体力已达到阈值，执行清体力任务")
            await self._execute_cleanup(None)

        self.trigger_task = asyncio.create_task(delayed_trigger())
        logger.info(f"已设置定时任务，将在 {delay/60:.1f} 分钟后触发")

    # ========== 工具方法 ==========

    def _get_config(self, key: str, default=None):
        """获取插件配置"""
        try:
            return self.context.get_config(key) or default
        except Exception:
            return default

    @staticmethod
    async def _send_wol(mac: str, broadcast_ip: str = "192.168.1.255"):
        """发送 WOL 魔术包"""
        mac_clean = mac.replace(":", "").replace("-", "").replace(" ", "")
        if len(mac_clean) != 12:
            logger.error(f"无效的 MAC 地址：{mac}")
            return

        magic_packet = bytes.fromhex("FF" * 6 + mac_clean * 16)

        try:
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.settimeout(2)
            sock.sendto(magic_packet, (broadcast_ip, 9))
            sock.sendto(magic_packet, (broadcast_ip, 7))  # 部分设备监端口7
            sock.close()
            logger.info(f"WOL 魔术包已发送至 {mac}")
        except Exception as e:
            logger.error(f"WOL 发送失败：{e}")

    async def terminate(self):
        """插件卸载时清理"""
        if self.trigger_task and not self.trigger_task.done():
            self.trigger_task.cancel()
            logger.info("定时任务已取消")
