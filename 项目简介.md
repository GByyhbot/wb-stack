# wb-stack 项目简介

**一句话**：WorkBuddy 多账号运维套件 —— 一个管账号（签到、积分、选号）的管理面板，加一个让多个账号轮流对外供活的 OpenAI 兼容网关。

多个 WorkBuddy 账号的积分各自独立、令牌还是轮换型的，手工维护很容易乱：谁还有积分、谁的令牌过期了、调用方该用哪个号。wb-stack 把这三件事自动化：面板负责把账号养好（每天签到、每小时查积分、异常告警），网关负责把账号用好（按优先级选号、耗尽自动切换、用量可查账）。

## 组成

| 组件 | 端口 | 作用 |
|---|---|---|
| **wb-manager** | 8300 | FastAPI 管理面板 + APScheduler 调度：账号增删改、每日自动签到、每小时刷新真实可用积分、当前账号查询/取号 API、OAuth 设备授权入库、企业微信异常告警。数据存 SQLite（WAL）。 |
| **wb-rotator** | 8310 | OpenAI 兼容轮换网关：账号池（`accounts.json`）、手动置顶、账号×模型级耗尽与限流冷却、SSE 透传与非流式聚合、按月 JSONL 用量记账。附带 `account_switch.py` 本机客户端登录态快照切号工具。 |
| **deploy/** | — | 单容器 Docker 部署（一个容器同时跑面板+网关），`deploy.py` 一键部署到 ECS，也可经环境变量改打 NAS。 |
| 辅助 | — | `export_second_rt.bat`：副账号「快照 + 令牌导出」一键脚本。 |

## 核心机制

- **积分口径**：可用积分 = 总发放 − 已用，走官方 `get-user-resource-summary` 精确查询；签到接口返回的累计总积分只作参考，绝不冒充余额（这是踩过坑后立的规矩，代码与测试里都有钉死）。
- **选号**：按 `priority` 升序（1 为主账号），跳过已停用与可用积分 ≤ 0 的账号；网关侧另有账号×模型级的硬耗尽（当日）与软冷却（限流后短时跳过），不整号拉黑。面板可手动置顶任意账号。
- **轮换令牌双向同步**：官方 refresh token 是轮换型的，面板与网关双写同一账号时，谁刷新谁负责把新令牌同步到另一份存储（面板→网关走 `app/gateway_sync.py`，网关→面板同机时经 `WB_DB_PATH` 回写）；`accounts.json` 的所有整表读写持有跨进程文件锁，不会互相覆盖。栈外第三方（如签到技能）仍不可共用同一把令牌。
- **刷新失败分类**：上游明确拒绝（令牌失效）才把账号标记耗尽；网络抖动只跳过本次请求，不误下线健康账号。
- **告警**：账号令牌异常、积分 3 天内到期、可用积分占比低于阈值，经企业微信群机器人推送（均带每日去重）。

## 访问与鉴权

- 面板：HTTP Basic（账号固定 `wbadmin`，密码见部署 `.env` 的 `MANAGER_PASSWORD`）。
- 网关：`Authorization: Bearer <ROTATOR_KEY>`，兼容 OpenAI 客户端（`/v1/chat/completions`、`/v1/models`），支持流式与工具调用。

## 部署形态

1. **NAS**：面板与网关各一个 systemd 服务（仓库内含 `wb-rotator.service`）。
2. **本机（Windows）**：网关可独立运行，`start_rotator.vbs` 无窗口后台启动。
3. **Docker 单容器**：`deploy/` 打包双服务 + frp/公网发布，`deploy.py` 负责上传、构建与验收。

## 技术栈

Python 3 · FastAPI · APScheduler · SQLite（WAL）· 原生 HTML/JS 前端（无构建）· Docker

## 测试

仓库根目录 `tests/` 有离线回归测试（上游接口全 mock，覆盖积分口径、选号、名称解析、令牌同步、失败分类等）：

```bash
pip install -r wb-manager/requirements.txt pytest
python -m pytest tests -q
```

## 安全提示

`accounts.json` 与面板数据库里都是可直接调用的长效令牌，只存放在自己机器上、切勿分享或提交到公开仓库；公网暴露时务必改强密码，并优先走 HTTPS/内网穿透的安全通道。
