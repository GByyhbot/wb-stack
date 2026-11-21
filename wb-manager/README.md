# WorkBuddy 多账号管理平台 (wb-manager)

部署在飞牛 NAS（192.168.31.95）上的 WorkBuddy 多账号管理工具。

## 访问

- Web 界面：http://192.168.31.95:8300 （局域网内浏览器直接打开）
- systemd 服务名：`wb-manager`（已设开机自启，崩溃自动拉起）

## 📖 核心概念：积分 vs 额度（层级依赖，必读）

系统中「积分」和「额度」是**两个不同层级**的资源，规则如下：

### 积分（Credits）—— 全局资源，第一道闸门

- **账号维度**的全局资源，账号内所有模型共享，不区分模型
- **积分是使用任何模型的前提条件**：账号可用积分耗尽（≤ 0）时，**所有模型均不可用**，即使各模型仍有剩余每日额度

### 额度（Quota）—— 模型每日限制，第二道闸门

- **每个模型每日独立分配**的用量，模型之间互不影响
- 某模型当日额度用尽后：该模型当日不可用，但**其他模型不受影响**，积分充足也不能绕过

### 层级依赖（两道闸门，全部通过才能调用）

```
请求 → 闸门1: 账号可用积分 > 0 ？ ──否──> 所有模型均不可用（全局拦截）
              │是
              ▼
       闸门2: 该模型当日额度剩余 > 0 ？ ──否──> 仅该模型不可用（其余模型正常）
              │是
              ▼
            放行调用
```

> 本平台的「可用积分」列监控的是**积分层**（闸门1），数据来自
> `POST /billing/meter/get-user-resource-summary` 的 `TotalCredits`（各套餐剩余之和，
> 与客户端"可用积分"一致）。注意：签到接口的 `total_credits` 是**累计总积分**，不是可用积分。
> 额度层（闸门2，模型×每日）当前未做强制限制；如需按模型设日限额可在代理层（wb-rotator）扩展。

## 功能

1. **自动签到**：每日定时（默认 08:30，可在 Web 设置）对所有 active 账号签到，记录日志与连续天数。
2. **积分管理**：每小时整点自动刷新各账号**真实可用积分**（get-user-resource-summary），Web 界面进度条可视化。
3. **账号自动切换**：`priority=1` 为主账号；可用积分 ≤ 0 的账号自动跳过，顺延到下一个有积分的账号。业务方调用：
   - `GET /api/active-account` → 当前应使用的账号
   - `POST /api/switch/consume` → 取号并排除指定账号

## ⚠️ 令牌机制（重要）

- 账号的 `token` 字段保存的是 **长效令牌（refresh token）**，不是短效 access token。
- 官方 refresh token 是**轮换型**：每次刷新会返回新的 refresh token，平台自动回写数据库。
- **同一把 refresh token 只能在一处使用**——例外只有本平台与 wb-rotator 网关这一对：两边双写同一账号令牌时已实现双向同步（谁刷新谁把新令牌同步到另一份存储，见 `app/gateway_sync.py` 与 rotator 的 `_sync_panel_db`），不会再各存一代互相刷死。但若同一账号还同时配置在签到技能等**栈外第三方**，仍会互相刷新导致失效——多账号请各自导出独立的 refresh token，栈外一处一个。
- 新增账号：在已登录 WorkBuddy 的电脑上运行
  `python ~/.workbuddy/skills/totorosir-workbuddy-checkin__skillhub/scripts/rt_auth.py --export-rt`
  （明文输出到终端，复制后在本平台 Web 界面添加账号时粘贴到 Token 栏）。
- 令牌失效（长期未刷新/被其他端轮换）：账号状态会变为 error，重新导出并更新该账号 token 即可。

## 已验证的官方接口（2026-11-06 实测）

| 用途 | 接口 |
|------|------|
| 刷新令牌 | `POST https://copilot.tencent.com/v2/plugin/auth/token/refresh`（头 X-Refresh-Token / X-Auth-Refresh-Source: plugin / X-Domain） |
| 签到状态 | `POST https://www.codebuddy.cn/v2/billing/meter/checkin-activity-status`（Bearer accessToken，返回 today_checked_in/streak_days；⚠️ 其 total_credits 是签到活动**累计总积分**，不是可用余额） |
| 签到领取 | `POST https://www.codebuddy.cn/v2/billing/meter/daily-checkin` |
| **真实可用积分** | `POST https://www.codebuddy.cn/billing/meter/get-user-resource-summary`（无 /v2 前缀！需浏览器形态 User-Agent，否则 WAF 403；`TotalCredits` = 可用积分） |

全部逻辑集中在 `app/workbuddy_client.py`，接口变化只改这一个文件。

## NAS 上运维命令

```bash
ssh Yyh154019@192.168.31.95   # 密码登录

# 服务管理
sudo systemctl status wb-manager
sudo systemctl restart wb-manager
sudo systemctl stop wb-manager

# 命令行管理
cd /vol1/1000/apps/wb-manager
./venv/bin/python cli.py list
./venv/bin/python cli.py add --name 主号 --user me@qq.com --token '<refresh_token>' --priority 1
./venv/bin/python cli.py checkin            # 立即全部签到
./venv/bin/python cli.py quota --refresh    # 刷新积分余额
./venv/bin/python cli.py switch             # 查看当前应使用账号
```

## 文件位置

- 代码：`/vol1/1000/apps/wb-manager/`
- 数据库（SQLite/WAL）：`/vol1/1000/apps/wb-manager/data/wb_manager.db`
- systemd 单元：`/etc/systemd/system/wb-manager.service`

## 回归测试

仓库根目录 `tests/` 有离线回归测试（2026-11-21 审计修复批配套，上游接口全 mock）：

```bash
pip install -r wb-manager/requirements.txt pytest
python -m pytest tests -q
```
