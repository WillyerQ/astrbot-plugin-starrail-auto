# 🌠 崩铁体力自动化 (starrail-auto)

> 崩坏：星穹铁道体力自动化管理插件 for AstrBot
> 自动计算体力恢复时间 → WOL 唤醒 Windows PC → SSH 通过计划任务运行三月七助手 → 自动关机 → 每日循环

## 功能概览

| 功能 | 说明 |
|------|------|
| ⏱ 体力计算 | 输入当前体力值，自动计算到达阈值（默认160）的精确时间 |
| 📡 WOL 网络唤醒 | 到点自动通过 etherwake/wakeonlan 唤醒你的 Windows PC |
| 🖥 SSH + 计划任务 | 通过 schtasks 在用户桌面会话中执行任务，**支持保留锁屏** |
| 🔄 任务可配置 | WebUI 选择要执行的任务（日常/周常/模拟宇宙等） |
| 🔌 自动关机 | 任务完成后自动关闭电脑，支持开关 |
| 🔁 每日循环 | 每天重置，重新计算新一轮触发时间 |

## 安装

**前置条件：**
- AstrBot v4.16+
- 目标 PC：Windows 10/11，开启 OpenSSH Server
- 目标 PC：支持 WOL（网卡 + BIOS 均需开启）
- 目标 PC：安装 [三月七助手](https://github.com/moesnow/March7thAssistant) + 崩坏星穹铁道
- AstrBot 所在机器：已安装 `etherwake` 或 `wakeonlan`（Docker 容器已预装）

**步骤：**
1. 插件放到 `AstrBot/data/plugins/` 下
2. `pip install paramiko>=4.0.0`
3. WebUI → 插件管理 → 重载插件
4. 填写配置

## 配置项

| 配置项 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| PC_IP | string | ✅ | 目标电脑内网IP |
| PC_MAC | string | ✅ | MAC地址（WOL用） |
| BROADCAST_IP | string | ❌ | WOL广播地址，默认自动从PC_IP推算（如192.168.31.206 → 192.168.31.255） |
| WOL_METHOD | string | ❌ | WOL方式：`udp`=容器直发(默认/etherwake)，`ssh`=通过NAS宿主机转发 |
| NAS_SSH_HOST | string | ❌ | 仅WOL_METHOD=ssh时，NAS SSH地址（默认127.0.0.1） |
| NAS_SSH_PORT | int | ❌ | 仅WOL_METHOD=ssh时，NAS SSH端口（默认22） |
| NAS_SSH_USER | string | ❌ | 仅WOL_METHOD=ssh时，NAS SSH用户名（默认root） |
| NAS_SSH_PASSWORD | string | ❌ | 仅WOL_METHOD=ssh时，NAS SSH密码 |
| PC_USERNAME | string | ✅ | Windows登录用户名 |
| PC_PASSWORD | string | ✅ | Windows登录密码 |
| MARCH7TH_PATH | string | ✅ | 三月七助手exe完整路径 |
| STARRAIL_PATH | string | ❌ | 崩铁游戏路径 |
| STAMINA_THRESHOLD | int | ❌ | 体力阈值，默认160 |
| SSH_PORT | int | ❌ | SSH端口，默认22 |
| AUTO_SHUTDOWN | bool | ❌ | 是否关机，默认true |
| SELECTED_TASKS | list | ❌ | 任务列表，默认["main"] |

**可选任务：** main（完整运行）、daily（每日实训）、weekly（周常）、universe_gui（模拟宇宙）、forgottenhall（忘却之庭）、echo_of_war（历战余响）、assignment（委托）、quest（任务）

## 指令

| 指令 | 说明 |
|------|------|
| `/体力设置 <数值>` | 初次设置体力，自动计算触发时间并设定时器 |
| `/体力状态` | 查询当前体力及下次触发时间 |
| `/清体力` | 手动触发：WOL → 计划任务执行三月七 → 关机 |
| `/体力重置` | 清除数据重新开始 |
| `/体力帮助` | 以图片形式显示指令列表（支持自定义背景） |

## 自定义帮助背景

在插件目录下的 `backgrounds/` 文件夹放入一张图片（PNG/JPG），`/体力帮助` 命令生成图片时会自动使用该图作为背景，并叠加半透明遮罩保证文字可读。

```bash
# 示例：放一张崩铁截图作为背景
cp 你的图片.png /root/AstrBot/data/plugins/astrbot_plugin_starrail_automation/backgrounds/
```

有多张图时按文件名排序取第一张。不放图则使用默认深色底色。

## 文件结构

```
astrbot_plugin_starrail_automation/
├── metadata.yaml          # 插件元数据
├── _conf_schema.json      # WebUI 配置模式
├── main.py                # 主逻辑
├── requirements.txt       # 依赖声明
├── README.md              # 本文件
└── backgrounds/           # 帮助图片自定义背景
    └── .gitkeep
```

## 体力算法

体力恢复速率：**1点 / 6分钟**（10点/小时）

所需时间 = (阈值 − 当前体力) × 6 分钟

## 执行原理（支持锁屏）

```
插件 → SSH → Windows PC
  ↓
schtasks /create（以用户身份，最高权限）
  ↓
schtasks /run → 在用户桌面会话（Session 1）中启动三月七
  ↓
即使控制台锁屏（Win+L），进程仍在用户会话中运行
  ↓
PyAutoGUI 可正常截屏+模拟点击
```

## 循环流程

输入体力 → 计算时间 → 设定时器 → 到点etherwake唤醒PC → 计划任务跑三月七 → 自动关机 → 每日重置

## 注意事项

1. 目标 PC 需一直插电
2. Windows **必须开启 OpenSSH Server**（设置 → 应用 → 可选功能 → 添加）
3. **无需设置自动登录**。插件通过计划任务 `schtasks /ru /rp` 以指定用户身份运行，可兼容 WOL 唤醒后的登录界面状态
4. 建议关闭睡眠和休眠：`powercfg /change standby-timeout-ac 0`
5. 三月七助手要求游戏分辨率 **1920×1080**，不支持 HDR
6. 重启 AstrBot 后体力数据会丢失，需重新 `/体力设置`
7. WOL 使用系统 `etherwake`（优先）或 `wakeonlan` 客户端发送唤醒包，Docker 容器已预装。如果运行在 host 网络模式下，UDP 直发也能正常工作

## FAQ

**Q: WOL 唤醒失败怎么办？**
A: 检查日志显示的是 `etherwake` 还是 `原始 UDP`。如果是 `原始 UDP`，说明容器内缺少 etherwake 工具。如果是 `etherwake` 成功但 PC 没醒，请检查：① PC 网卡是否开启 WOL（设备管理器 → 网卡 → 高级 → 魔术包唤醒）② BIOS 是否开启 WOL ③ 广播地址是否正确（看日志中的 IP 网段）

**Q: Docker 容器网络怎么配？**
A: 默认 `wol_method=udp` 即可，插件会优先用系统 etherwake 工具发送，无需 host 网络模式。如果仍失败，可尝试 `wol_method=ssh` 通过宿主机转发。