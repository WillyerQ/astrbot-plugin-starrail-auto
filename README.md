# 🌠 崩铁体力自动化 (starrail-auto)

> 崩坏：星穹铁道体力自动化管理插件 for AstrBot
> 自动计算体力恢复时间 → WOL 唤醒 Windows PC → SSH 通过计划任务运行三月七助手 → 自动关机 → 每日循环

## 功能概览

| 功能 | 说明 |
|------|------|
| ⏱ 体力计算 | 输入当前体力值，自动计算到达阈值（默认160）的精确时间 |
| 📡 WOL 网络唤醒 | 到点自动发送魔术包唤醒你的 Windows PC |
| 🖥 SSH + 计划任务 | 通过 schtasks 在用户桌面会话中执行任务，**支持保留锁屏** |
| 🔄 任务可配置 | WebUI 选择要执行的任务（日常/周常/模拟宇宙等） |
| 🔌 自动关机 | 任务完成后自动关闭电脑，支持开关 |
| 🔁 每日循环 | 每天重置，重新计算新一轮触发时间 |

## 安装

**前置条件：**
- AstrBot v4.16+
- 目标 PC：Windows 10/11，开启 OpenSSH Server
- 目标 PC：支持 WOL，**需设置自动登录**（netplwiz）
- 目标 PC：安装 [三月七助手](https://github.com/moesnow/March7thAssistant) + 崩坏星穹铁道

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

- `/体力设置 <数值>` — 初次设置体力，自动计算触发时间并设定时器
- `/体力状态` — 查询当前体力及下次触发时间
- `/清体力` — 手动触发：WOL → 计划任务执行三月七 → 关机
- `/体力重置` — 清除数据重新开始

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

输入体力 → 计算时间 → 设定时器 → 到点WOL唤醒PC → 计划任务跑三月七 → 自动关机 → 每日重置

## 注意事项

1. 目标 PC 需一直插电
2. Windows **必须开启 OpenSSH Server**（设置 → 应用 → 可选功能 → 添加）
3. **支持保留锁屏**。插件通过计划任务 `schtasks` 在用户桌面会话执行，不依赖控制台是否解锁
4. 电脑需设置**自动登录**（`netplwiz` 取消密码勾选），否则 WOL 唤醒后会停在登录界面
5. 建议关闭睡眠和休眠：`powercfg /change standby-timeout-ac 0`
6. 三月七助手要求游戏分辨率 **1920×1080**，不支持 HDR
7. 重启 AstrBot 后体力数据会丢失，需重新 `/体力设置`
