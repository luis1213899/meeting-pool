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

## 四、和腾讯会议对账（这块最重要）

号池如果只信自己的账本，就会出两种事故。所以每 10 分钟（可调）自动跟腾讯会议对一次账，也可以点「同步会议」手动触发。

### 事故一：号主自己约的会，号池不知道

号主在自己的腾讯会议 App 里约了今晚 22:00 的会，号池却显示这个号今晚空着 → 员工借走 → 撞车。

**现在**：对账会把账号下「未开始 / 进行中」的会议全部拉回来，记入 `external_meetings`：

- 员工端时间轴用**橙色虚线块**显示，标「上游 · 主题」
- 分配时**按占用处理**，同一时段不会再借给第二个人
- 这些会**不占**账号的每日限额（那是号主自己的会）

### 事故二：本地取消了，腾讯会议里还在

原来的实现有个真 bug：上游取消失败时只往备注里写一句「上游取消失败（本地已释放）」，
状态照样标成已取消，号也释放了 —— 表面成功，实际那场会还在。

**现在**改成「上游成功才算成功」：

| 情况 | 行为 |
| --- | --- |
| 上游取消成功 | 本地标记已取消，状态 `cancelled` |
| 上游取消失败 | **不动本地状态**，号继续占着，把原因抛给操作人 |
| 管理员点「强制释放」 | 本地释放，但记 `cancel_failed`，后台顶部出现待处理提醒 |
| 之后点「重新取消」 | 真的再打一次上游，成功才清掉提醒 |

### 有一种取消是「做不到」，不是「失败」

腾讯会议**不允许取消已经开始的会议**（哪怕还没到预约开始时间，只要有人提前入会开了，也会变成不可取消）。
这时正确做法不是取消，而是在会里点「结束会议」。所以：

- 预约已开始 → 直接释放号码，提示「会议已开始，腾讯不允许取消；号码已释放，请在会议里点结束会议」
- 上游回 `已开始的会议不能取消` → 同样按上面处理，不会给员工弹一个吓人的报错

### 为什么取消完在 App 里还能看到那场会

腾讯会议会把**已取消的会议也留在列表里**，状态显示「已取消」。这是留档，不是没取消。
判断标准是「还在不在**未开始/进行中**列表里」，而不是「App 里搜不搜得到」。

对账就是按这个标准判断的：已经不在活动列表里的，会顺手把历史误报清掉。

### 相关配置

| 设置 | 默认 | 说明 |
| --- | --- | --- |
| `sync_minutes` | 10 | 自动对账间隔（分钟）。设 0 关闭，只能手动同步 |

环境变量 `POOL_DISABLE_SCHEDULER=1` 可整体关掉后台线程（测试或多实例调试时用）。

---

## 五、号池是怎么分号的

分配逻辑（`pool/service.py`）：

1. **可用性**：状态必须 `active`（暂停/停用不参与），并且凭证齐全（mcp 有 Token / manual 有会议号）
2. **时长**：本次时长 ≤ 该账号的「单场最长」
3. **日限额**：该账号当天已借场次 < 「每日上限」
4. **时间冲突**：与该账号已有预约**不重叠**，并在前后各留 `buffer_minutes` 的交接缓冲
5. **上游占用**：该账号在腾讯会议里已有的会议（号主自己约的）也不能重叠
6. **个人配额**：借用人近 7 天借号次数 < `max_per_week`
7. **排序**：当天用得最少的优先 → 会员类型高的优先 → 编号小的优先（天然负载均衡）

预约失败会**自动换号重试**（最多 3 个）。驱动抛错时，这次预约记 `failed` 并给该账号打 `warn`，不影响其他号。整段逻辑用 SQLite `BEGIN IMMEDIATE` 串行化，两个人同一秒抢同一个时段也不会重复分配。

默认规则（后台「借用规则」可改，改完即时生效）：

| 规则 | 默认 | 说明 |
| --- | --- | --- |
| 场间缓冲 | 5 分钟 | 防止上一场拖堂，下一场接不上 |
| 每人每周 | 5 次 | 防止一个人长期霸占 |
| 提前预约 | 14 天 | 窗口上限 |
| 单场时长 | 15–240 分钟 | 更长会议请走正式流程或调高账号上限 |
| 可借时段 | 8:00–23:00 | 避开凌晨 |
| 对账间隔 | 10 分钟 | 拉上游会议、核验取消；0 表示关闭自动对账 |

---

### 借号时能选的主持人选项

员工借号时会看到三个开关，默认值按“最省事又不添麻烦”定：

| 选项 | 默认 | 对应上游参数 |
| --- | --- | --- |
| 云录制 | **开** | `auto_record_type: cloud`（关掉是 `none`） |
| 等候室 | 关 | `auto_in_waiting_room: true/false` |
| 入会密码 | **关**（不设） | 不传 `password`；打开后必须填 4–6 位数字 |

- 账号没有录制权限时不会让整场预约失败：自动降级为不录制，并把结果如实记在预约上（结果卡显示「未开录制」）。
- 密码为空时邀请文案里直接不出现密码行，避免发出去一条“入会密码：”后面空着。
- 开关会落库（`record_enabled` / `waiting_room`），所以历史记录能看出这场会当时开没开录制。

### 排期甘特图

顶部第二个页签就是独立的甘特图页面（`/#/gantt`），借号页里的小图只是缩略版，右上「完整甘特图」可跳过来。

- 一行一个号，横轴按小时铺开；时间范围会随数据自动扩展（默认 8:00–23:00，有更早/更晚的会自动带上）
- 色块按状态分色：号池借出（蓝）、进行中（绿）、已结束（灰）、上游已约（橙色斜纹）
- 色块内两行显示：开始时间 + 主题；太窄的块自动只留色块，细节靠点击
- **点色块**看这个时段的详情（主题 / 时段 / 占用方 / 出号账号 / 状态）
- **点空白处**会告诉你这个号在这个时间空着，并能一键把日期+时间填进借号表单（自动切回借号页）
- 今天会画一条红色当前时间线
- 手机上横向滚动、账号名吸附在左侧，色块不显示文字，轨道加高方便点按

---

## 六、安全边界（请认真读）

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

## 七、部署到服务器

### 本仓库的实际线上形态（LoveVPS）

| 项目 | 值 |
| --- | --- |
| 代码目录 | `/opt/meeting-pool/`（虚拟环境 `.venv/`，数据 `data/pool.db`） |
| 服务 | `meeting-pool.service` → `127.0.0.1:8099` |
| 公网入口 | `https://your-domain.com/tenxunhuiyiyuding/`（nginx 反代并剥前缀） |
| 旧形态 | 同路径的静态「腾讯会议预约」页 + `tenxunhuiyiyuding.service`（18084），2026-10-18 已替换并删除 |

部署资产都在 `deploy/`：

```bash
# 1) 上传（排除数据与密钥）
tar -czf - --exclude=./data --exclude=./.env --exclude=./.venv --exclude=./output . \
  | ssh your-server "tar xzf - -C /opt/meeting-pool"

# 2) 依赖
ssh your-server "cd /opt/meeting-pool && .venv/bin/pip install -q -r requirements.txt"

# 3) 重启
ssh your-server "systemctl restart meeting-pool && systemctl is-active meeting-pool"

# 4) nginx 路由（幂等，自带备份与 nginx -t 失败回滚）
ssh your-server "python3 /opt/meeting-pool/deploy/patch_nginx.py"
```

关键环境变量（`.env`，权限 600，属主必须是运行服务的用户）：

```ini
POOL_PORT=8099
POOL_DB_PATH=/opt/meeting-pool/data/pool.db
POOL_ADMIN_PASSWORD=...      # 管理后台口令
POOL_MASTER_KEY=...          # 32 字节 base64，决定 Token 能否解密
```

员工端不登录（内部工具，拿到链接就能借）；管理端走 `POOL_ADMIN_PASSWORD`。

### 放进工作台

`deploy/meeting-pool-entry.js` 会在运行时往侧栏「聊天」组和概览页快捷入口各加一项「会议号池」。
选择注入而不是改 React 源码，是因为线上 bundle 与本地源码不同源，重建会把未发布的改动带上线。
撤掉它只需要删掉 `relay-landing/index.html` 里那一行 `<script src="/relay-assets/meeting-pool-entry.js">`。

### 通用 systemd / nginx 示例

```ini
[Unit]
Description=Meeting Pool
After=network-online.target

[Service]
Type=simple
User=meeting
WorkingDirectory=/opt/meeting-pool
EnvironmentFile=/opt/meeting-pool/.env
ExecStart=/opt/meeting-pool/.venv/bin/python app.py
Restart=always
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

```nginx
location = /tenxunhuiyiyuding { return 302 /tenxunhuiyiyuding/; }
location ^~ /tenxunhuiyiyuding/ {
    proxy_pass http://127.0.0.1:8099/;   # 末尾斜杠负责剥前缀
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;
    add_header Cache-Control "no-store" always;
}
```

备份只需要一个文件：`data/pool.db`（连同 `-wal` / `-shm` 一起拷，或先停服务）。

---

## 八、接口一览

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
| POST | `/api/admin/sync` | 立即对账：拉上游会议 + 核验取消 |
| GET | `/api/admin/external` | 上游已有会议列表 |
| GET | `/api/admin/problems` | 上下游不一致的预约 |
| POST | `/api/admin/bookings/{id}/retry-cancel` | 重新取消（针对取消失败） |
| GET | `/api/admin/bookings` | 预约查询（日期/状态/关键词） |
| POST | `/api/admin/bookings/{id}/cancel` | 强制结束并释放 |
| GET | `/api/admin/export.csv` | 导出 |
| GET | `/api/admin/audit` | 操作日志 |
| GET/PATCH | `/api/admin/config` | 借用规则 |

`/api/docs` 有自动生成的 OpenAPI 文档。

---

## 九、已知边界（别踩坑）

- **免费版会议有 40 分钟限制**，这是账号侧的规则，号池改不了。想开长会，号池里得有专业版/商业版账号。
- **个人专属会议号暂不支持 API 创建**（官方能力在建）。所以每次借号都是**新建一场会**，会议号每次不同——这反而更适合借号场景，用完即弃。
- **主持人是号主那个账号**。员工拿到的是“参会者”身份，需要主持人操作时（云录制、锁定会议、转让主持），要么号主协助，要么员工用该账号登录 App。这是 MCP 权限模型决定的，不是本项目能绕过的。
- **MCP 的路径与工具名可能调整**。当前默认 `https://mcp.meeting.tencent.com/mcp/wemeet-open/v1`（官方 Skill 包 `config.json` 的 baseUrl）。若哪天变了，「MCP 自检」会把 5 个候选地址全试一遍，把可用的那个填进 `POOL_MCP_URL` 即可，不用改代码。
- 企业版/商业版账号目前需要向腾讯申请开通 MCP 能力。

---

## 十、目录结构

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

## 十一、测试

```bash
python -m pytest          # 57 passed
```

覆盖：Token 加解密与掩码、口令哈希、MCP 缺 Token 校验、固定会议号校验、正常分配、同号冲突、缓冲期、负载均衡、日限额、周配额、时长上下限、过去时间、非工作时段、取消释放、取消凭证、上游失败换号、状态推进、看板形状、CSV 导出、登录鉴权、接口全流程，MCP 两层包装解析（成功 / `token not found` / 业务 4xx / `isError`）、`time_zone` 与 `_client_info` 注入、参数为空自动剔除，以及**对账**：上游会议导入、上游会议占位导致分号被拒、取消失败不释放号码、强制释放留标记、重新取消清除标记、已开始会议按「结束会议」处理且不误报。
