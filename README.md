# 会议号池（Meeting Pool）

把团队里零散的腾讯会议会员号集中起来，变成一个**随借随用的号码池**：员工填三行字，系统自动挑一个空闲的号、当场开出会议号，拿到的就是会议号 + 入会链接 + 可直接转发的邀请文案。没会员的同事不用再到处问“谁的号能借我用一下”。

> 底层用的就是腾讯会议官方 MCP（`schedule_meeting` / `cancel_meeting` / `get_user_meetings`），每个账号用自己的 Token，系统只做调度与记账，不代理会议内容。

---

## 一、它到底解决什么

| 过去 | 现在 |
| --- | --- |
| 群里喊“谁的会员号借一下” | 打开页面，填时间，点一下 |
| 号主在开会，号被重复借走 | 系统按时间片锁定，同一账号不会撞车 |
| 谁借了、借多久没人记得 | 台账 + 审计日志 + 每人每周配额 |
| 号多了没人管，Token 到处贴 | Token 加密落库，界面只显示尾号 |
| 临时会开完占着号 | 一键结束，号码立刻回池 |

设计上把它当成**图书馆借书**：号是书，时间段是借期，缓冲期是还书整理时间，配额是借阅上限。

---

## 二、三步跑起来

```bash
cd meeting-pool

# 1) 依赖（本机已有 Python 3.12 + fastapi/uvicorn/cryptography/httpx 可跳过）
pip install -r requirements.txt

# 2) 配置：改管理口令
copy .env.example .env      # Windows
# cp .env.example .env      # macOS/Linux

# 3) 起服务
python app.py
```

打开 http://127.0.0.1:8099 —— 员工页就是首页，「号池管理」在右上角。

想先看效果、还没有真实 Token：

```bash
python seed_demo.py --reset --bookings
```

会造 5 个演示账号 + 今天的占用，界面立刻是“在用的样子”。演示账号走 `mock` 驱动，不会连网。

看完效果、要接真实号了，把这些演示数据清掉（真实账号、真实预约、真实日志都不会被动）：

```bash
python seed_demo.py --purge            # 先预演，列出会删什么
python seed_demo.py --purge --apply    # 确认后真删
```

`--purge` 清理三类：mock 演示号与 seed 固定号及其预约、名称含「冒烟 / 自检 / 示例」的联调夹具账号、
以及指向已删除账号或预约的孤儿审计记录。

---

## 三、接入真实的腾讯会议账号

### 每个号需要准备什么

1. 用**该账号**登录 <https://meeting.tencent.com/ai-skill.html>（必须是个人账号，企业账号先到会议官网右上角切成个人账号）
2. 复制页面上的 Token（长期有效，重置后旧 Token 立刻失效）
3. 进「号池管理」→「+ 新增账号」→ 填名称、提供者、选「MCP 接口」→ 粘贴 Token → 保存
4. 点该行的「检测」，健康列变绿 `正常` 即为通了；如果报错，点右上「MCP 自检」看是地址问题还是 Token 问题

Token 会用 AES-256-GCM 加密后写入 SQLite，接口层永不回传明文，列表里只显示 `••••尾号`。

### 关于 MCP 地址与参数（已按官方核对）

官方端点是（来自 Skill 包 `config.json` 的 `baseUrl`）：

```
https://mcp.meeting.tencent.com/mcp/wemeet-open/v1
```

注意不是 `/mcp`。常见坑：`/mcp`、`/sse` 一律 404；正确路径在**没有 Token** 时会回 401 或 500，
所以「看到 404」等于路径错，「看到 401/500」等于路径对但缺 Token。

工具名与字段同样按官方 `tools/list`（共 36 个工具）对齐，可直接改 `.env`：

```ini
POOL_MCP_TOOL=schedule_meeting
POOL_MCP_CANCEL_TOOL=cancel_meeting
POOL_MCP_LIST_TOOL=get_user_meetings
POOL_MCP_TOKEN_HEADER=X-Tencent-Meeting-Token
POOL_MCP_SKILL_VERSION=v1.0.14
POOL_MCP_ARGS={"subject":"{subject}","start_time":"{start}","end_time":"{end}","password":"{password}","time_zone":"{timezone}"}
```

- 官方字段是 `time_zone`（带下划线），不是 `timezone`
- `_client_info`（os / agent / model）是必填系统参数，代码里自动注入，不用手填
- 返回是**两层包装**：`result.content[].text` → JSON → `data.body` 再是 JSON 字符串，真正的错误码在 `data.status_code` / `body.error` 里，HTTP 层永远 200。驱动层已经把这层剥开了，`token not found` 会翻译成「Token 无效或已失效」而不是静默失败。
- 建会返回里若有 `host_key`（主持人密钥），会存下来并显示在员工结果卡上——用它可以在腾讯会议里取得主持人权限，比只当参会者强。

### 自检工具

后台右上「MCP 自检」会同时试 5 个候选端点，逐行给出：HTTP 状态、握手结果、能列出多少工具、Token 是否真能调通、结论。

| 结论 | 含义 |
| --- | --- |
| 可用 | 地址对、工具能列、Token 能实调 |
| 地址对，Token 有问题 | 路径没问题，去重新取 Token |
| 地址对（带 Token 才能验证） | 没填 Token，路径已确认存在 |
| 地址错误 | 该端点 404 |

### 三种接入方式

| 驱动 | 适用 | 说明 |
| --- | --- | --- |
| `mcp` | 有 Token 的会员号 | 真实调用腾讯会议建会，最推荐 |
| `manual` | 号主有固定会议号 | 不连网，系统只登记号码并做排期；适合“常设会议室” |
| `mock` | 演示 / 联调 | 本地伪造会议号，流程完整 |

`manual` 很实用：有些同事的号已经开好了常设会议（固定 9 位号），直接把号登记进来，系统负责“这个时段归谁用”，一样不会撞车。

---

## 四、号池是怎么分号的

分配逻辑（`pool/service.py`）：

1. **可用性**：状态必须 `active`（暂停/停用不参与），并且凭证齐全（mcp 有 Token / manual 有会议号）
2. **时长**：本次时长 ≤ 该账号的「单场最长」
3. **日限额**：该账号当天已借场次 < 「每日上限」
4. **时间冲突**：与该账号已有预约**不重叠**，并在前后各留 `buffer_minutes` 的交接缓冲
5. **个人配额**：借用人近 7 天借号次数 < `max_per_week`
6. **排序**：当天用得最少的优先 → 会员类型高的优先 → 编号小的优先（天然负载均衡）

预约失败会**自动换号重试**（最多 3 个）。驱动抛错时，这次预约记 `failed` 并给该账号打 `warn`，不影响其他号。整段逻辑用 SQLite `BEGIN IMMEDIATE` 串行化，两个人同一秒抢同一个时段也不会重复分配。

默认规则（后台「借用规则」可改，改完即时生效）：

| 规则 | 默认 | 说明 |
| --- | --- | --- |
| 场间缓冲 | 5 分钟 | 防止上一场拖堂，下一场接不上 |
| 每人每周 | 5 次 | 防止一个人长期霸占 |
| 提前预约 | 14 天 | 窗口上限 |
| 单场时长 | 15–240 分钟 | 更长会议请走正式流程或调高账号上限 |
| 可借时段 | 8:00–23:00 | 避开凌晨 |

---

## 五、安全边界（请认真读）

- **Token 等于那个账号的身份**。拿到 Token 的人能以号主名义建会、改会、取消会、读录制和纪要。所以：
  - 主密钥 `POOL_MASTER_KEY` 只放服务器环境变量或 `.pool_secret`（首次启动自动生成，权限 600），**不要提交 Git**
  - 怀疑泄露，去 ai-skill 页面点「重置」，然后把新 Token 填回后台
  - 本项目不记录 Token 明文，日志里也没有
- **管理口令**：`POOL_ADMIN_PASSWORD` 必须改。仍用默认值时登录会弹提示。口令用常量时间比较（`hmac.compare_digest`）并在进程内以 scrypt 缓存校验值。
- **员工端不登录**：内部工具，只有“借号”和“我的预约”。如果你需要实名，靠填写的姓名/部门 + 审计日志追溯。要接企业微信/飞书登录，在 `pool/api.py` 里加一层 `require_user` 即可。
- **员工取消凭证**：每笔预约生成一个 `manage_token`，只存在于借用人的浏览器 local storage 和返回链接里，别人拿不到就不能改你的预约。
- **建议只监听 127.0.0.1**，前面挂 Nginx 做 HTTPS 和 `X-Forwarded-For`。默认配置就是这样。
- **限流**：借号 12 次/分钟/IP，登录 8 次/5 分钟/IP。

---

## 六、部署到服务器

systemd 单元（示例）：

```ini
[Unit]
Description=Meeting Pool
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/meeting-pool
ExecStart=/usr/bin/python3 app.py
Restart=always
Environment=POOL_HOST=127.0.0.1
Environment=POOL_PORT=8099
# 口令与主密钥建议放 EnvironmentFile，权限 600
EnvironmentFile=/opt/meeting-pool/.env
User=www-data

[Install]
WantedBy=multi-user.target
```

Nginx：

```nginx
server {
    listen 443 ssl http2;
    server_name meeting.your-domain.com;

    location / {
        proxy_pass http://127.0.0.1:8099;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

备份只需要一个文件：`data/pool.db`（连同 `-wal` / `-shm` 一起拷，或先停服务）。

---

## 七、接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/state` | 号池概况、规则（员工端） |
| GET | `/api/timeline?date=` | 某天各账号占用块 |
| POST | `/api/bookings` | 借号 |
| GET | `/api/bookings/{id}?token=` | 查单笔 |
| POST | `/api/bookings/{id}/cancel` | 结束/取消（带 manage_token） |
| POST | `/api/my` | 用本地 token 列表批量查 |
| POST | `/api/admin/login` `/logout` `/me` | 管理会话 |
| GET | `/api/admin/overview` | 看板数据 |
| GET/POST/PATCH/DELETE | `/api/admin/accounts...` | 台账增删改查 |
| POST | `/api/admin/accounts/{id}/probe` | Token 健康检测 |
| POST | `/api/admin/mcp/diagnose` | MCP 端点自检（候选地址 + Token 实调） |
| GET | `/api/admin/bookings` | 预约查询（日期/状态/关键词） |
| POST | `/api/admin/bookings/{id}/cancel` | 强制结束并释放 |
| GET | `/api/admin/export.csv` | 导出 |
| GET | `/api/admin/audit` | 操作日志 |
| GET/PATCH | `/api/admin/config` | 借用规则 |

`/api/docs` 有自动生成的 OpenAPI 文档。

---

## 八、已知边界（别踩坑）

- **免费版会议有 40 分钟限制**，这是账号侧的规则，号池改不了。想开长会，号池里得有专业版/商业版账号。
- **个人专属会议号暂不支持 API 创建**（官方能力在建）。所以每次借号都是**新建一场会**，会议号每次不同——这反而更适合借号场景，用完即弃。
- **主持人是号主那个账号**。员工拿到的是“参会者”身份，需要主持人操作时（云录制、锁定会议、转让主持），要么号主协助，要么员工用该账号登录 App。这是 MCP 权限模型决定的，不是本项目能绕过的。
- **MCP 的路径与工具名可能调整**。当前默认 `https://mcp.meeting.tencent.com/mcp/wemeet-open/v1`（官方 Skill 包 `config.json` 的 baseUrl）。若哪天变了，「MCP 自检」会把 5 个候选地址全试一遍，把可用的那个填进 `POOL_MCP_URL` 即可，不用改代码。
- 企业版/商业版账号目前需要向腾讯申请开通 MCP 能力。

---

## 九、目录结构

```
meeting-pool/
├── app.py                 启动入口（python app.py）
├── pool/
│   ├── config.py          .env 读取 + 本地密钥
│   ├── db.py              SQLite 结构、设置、审计
│   ├── security.py        Token 加密 / 口令 / 会话签名
│   ├── drivers.py         mcp / manual / mock 三种驱动
│   ├── service.py         分配算法、借还、看板
│   └── api.py             HTTP 接口 + 后台定时推进状态
├── static/                员工端 + 管理后台（无构建步骤）
├── tests/                 35 个用例（分配/冲突/配额/加密/接口）
├── seed_demo.py           演示数据
├── requirements.txt
└── .env.example
```

## 十、测试

```bash
python -m pytest          # 45 passed
```

覆盖：Token 加解密与掩码、口令哈希、MCP 缺 Token 校验、固定会议号校验、正常分配、同号冲突、缓冲期、负载均衡、日限额、周配额、时长上下限、过去时间、非工作时段、取消释放、取消凭证、上游失败换号、状态推进、看板形状、CSV 导出、登录鉴权、接口全流程，以及 MCP 两层包装解析（成功 / `token not found` / 业务 4xx / `isError`）、`time_zone` 与 `_client_info` 注入、参数为空自动剔除。
