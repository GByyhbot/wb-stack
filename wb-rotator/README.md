# WorkBuddy 本机多账号自动轮换代理 (wb-rotator)

本机运行的 OpenAI 兼容网关：用账号池调用 `glm-5.3-flash` / `deepseek-v4.1-flash`，
当前账号每日额度耗尽时**自动切换下一个账号**，直到所有账号耗尽（次日自动恢复）。

## 📖 核心概念：积分 vs 额度（层级依赖，必读）

系统中「积分」和「额度」是**两个不同层级**的资源，规则如下：

### 积分（Credits）—— 全局资源，第一道闸门

- **账号维度**的全局资源，账号池内共享，不区分模型
- **积分是使用任何模型的前提条件**：账号可用积分耗尽（≤ 0）时，**所有模型均不可用**
- 即使各模型仍有剩余每日额度，积分耗尽照样全线不可用

### 额度（Quota）—— 模型每日限制，第二道闸门

- **每个模型每日独立分配**的用量，模型之间互不影响
- 某模型当日额度用尽后：该模型当日不可用，但**其他模型不受影响**
- 积分再充足也不能绕过模型层的每日额度限制

### 层级依赖关系（两道闸门，全部通过才能调用）

```
请求 → 闸门1: 账号可用积分 > 0 ？ ──否──> 所有模型均不可用（全局拦截）
              │是
              ▼
       闸门2: 该模型当日额度剩余 > 0 ？ ──否──> 仅该模型不可用（其余模型正常）
              │是
              ▼
            放行调用
```

> 当前的耗尽判定与切换基于**积分层**（`get-user-resource-summary` 返回的真实可用积分）。
> 模型级每日额度（闸门2）如需强制执行，可在 `rotator.py` 中扩展按「账号×模型×日期」记账（需先确定各模型的每日限额数值）。

## 快速使用

```bash
cd D:/workbuddy-tmp/2026-11-06-12-15-29/wb-rotator

# 1. 添加账号（refresh token 导出方式见下）
python rotator.py add --name 主号  --rt '<token1>' --priority 1
python rotator.py add --name 备用1 --rt '<token2>' --priority 2

# 2. 启动代理
python rotator.py serve --port 8310

# 3. 像调 OpenAI 一样用
curl http://127.0.0.1:8310/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"model":"glm-5.3-flash","messages":[{"role":"user","content":"hi"}]}'
```

Python（OpenAI SDK）：
```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8310/v1", api_key="anything")
resp = client.chat.completions.create(
    model="deepseek-V4.1-flash",   # 大小写会自动标准化
    messages=[{"role": "user", "content": "hi"}],
)
print(resp.model_extra["rotator"])   # 本次使用的账号
```

## 账号管理命令

```bash
python rotator.py list            # 查看账号池与今日状态
python rotator.py check           # 立即查询各账号积分余额（耗尽的自动标记）
python rotator.py reset           # 手动清除今日耗尽标记（调试用）
python rotator.py remove --name x # 移除账号
```

## 切换机制

1. 请求到来 → 按优先级取第一个「今日未耗尽」的账号
2. 用其 refresh token 换 access token（30 分钟缓存），转发到官方流式接口
3. 官方返回 402/429 或额度类错误 → **二次确认**：查余额接口的真实可用积分
   （`POST /billing/meter/get-user-resource-summary`，注意 WAF 要求浏览器 UA）
   - 确认可用积分 ≤ 0 → 标记该账号当日耗尽 → 自动切下一账号重试（最多 5 次）
   - 积分未确认耗尽（疑似限流/模型日额度问题）→ 不切换，把错误返回给客户端
4. 所有账号耗尽 → 返回 503 `all_accounts_exhausted`，次日自动恢复
5. 非流式请求（stream=false）由代理内部聚合 SSE 后返回完整 OpenAI JSON

## 🔀 一键切号（快照方案，借鉴 Buddy Switch）

原理：客户端登录态整体存在 `workbuddy-desktop.info`（加密信封仅本机可解），
切号 = 用目标账号登录时保存的**整份快照**覆盖该文件，无需解密令牌。

```bash
# 1. 登录某账号时保存快照（含 rt 导出，一次操作长期复用）
python account_switch.py snapshot --name 主号
python account_switch.py snapshot --name 备用1

# 2. 查看快照与当前登录账号
python account_switch.py list

# 3. 切号：先完全退出客户端（托盘也退出），然后
python account_switch.py switch --name 备用1
python account_switch.py launch        # 再启动客户端

# 4. 切回同样操作；每次切号前自动备份当前登录态（写失败自动回滚）
```

要点（与 Buddy Switch 相同的三条纪律）：
- **写前必备份**：当前登录态先存 `_before_switch.info`，失败自动回滚
- **客户端运行中拒绝写入**：内存中的旧登录态会把切号结果覆盖回去，必须先退出（`--force` 强制大概率无效）
- 快照仅本机有效（加密信封绑定本机），不要拷到别的电脑用

或直接双击项目根目录的 `export_second_rt.bat`：输入代号即完成「快照+令牌导出」。

## ⚠️ 令牌注意事项（同 NAS 平台）

- 配置里的 `refresh_token` 是**轮换型**：代理每次刷新会自动把新令牌写回 `accounts.json`，并（同机部署时经 `WB_DB_PATH`）同步回 NAS wb-manager 平台的数据库；`accounts.json` 的整表读-改-写持有跨进程文件锁（`accounts.json.lock`），面板/CLI 并发写不会互相覆盖。
- **同一把 refresh token 只能在一处使用**——例外只有本代理与 wb-manager 平台这一对（已双向同步，见上）。若同一账号还配置在本机签到技能等**栈外第三方**，仍会互相刷新导致其他边失效。多账号请各自导出独立 token，栈外一处一个。
- 导出 refresh token（在已登录的电脑上）：
  `python ~/.workbuddy/skills/totorosir-workbuddy-checkin__skillhub/scripts/rt_auth.py --export-rt`

## 文件

- `rotator.py` —— 全部逻辑（代理 + CLI）
- `accounts.json` —— 账号池（含轮换后的最新令牌，勿分享）
- 官方接口地址与耗尽判定关键字都写在 `rotator.py` 顶部常量区

## 开机自启（可选）

任务计划程序里建一个「登录时运行」任务：
`python D:\workbuddy-tmp\2026-11-06-12-15-29\wb-rotator\rotator.py serve --port 8310`
