# ChatNest - 诗的专属部署

## 项目概述
ChatNest 是一个开源 Claude 聊天 Web 应用，fork 自 [ugui3u/chatnest](https://github.com/ugui3u/chatnest)。
使用 Claude Agent SDK 模式（claude.py），通过诗的 Claude 订阅配额运行，不是 API 按量付费。

## 服务器信息
- **平台**: Zeabur 托管的 Tencent Ashburn VPS (2C 2GB)
- **IP**: 43.166.205.144
- **端口**: 8787
- **访问地址**: http://43.166.205.144:8787
- **SSH 用户**: ubuntu
- **项目路径**: `/home/ubuntu/chatnest/full-stack`
- **Python 虚拟环境**: `/home/ubuntu/chatnest/full-stack/venv`
- **数据库**: `/home/ubuntu/chatnest/full-stack/conversations.db` (SQLite)
- **访问密码**: 存于 `.env` 的 `CHAT_PASSWORD`

## Systemd 服务
两个服务，开机自启，崩溃自动重启：

```bash
# 主服务
sudo systemctl restart chatnest
sudo journalctl -u chatnest -n 50 --no-pager

# 记忆搜索服务
sudo systemctl restart chatnest-memory
sudo journalctl -u chatnest-memory -n 50 --no-pager
```

服务配置文件：
- `/etc/systemd/system/chatnest.service` — uvicorn 主应用 (端口 8787)
- `/etc/systemd/system/chatnest-memory.service` — memory_search_service (端口 3900)

## 服务器上的本地修改（未提交到 repo）
以下文件在服务器上有本地修改，和 repo 版本不同：
- `full-stack/models.json` — 移除了 Codex 和旧模型，修正了 Opus 4.5 和 Sonnet 4.5 的模型 ID 日期
- `full-stack/run.sh`, `run-memory-search.sh`, `run-memory-vectorize.sh` — 可能有本地路径调整

**重要**: 服务器上的 `index.html` 应该和 repo 保持一致。之前因为 git stash 合并冲突反复出问题，最终用 `git checkout origin/main -- full-stack/static/index.html` 解决。以后修改 index.html 时，优先在 repo 里改好再 pull，避免直接在服务器上编辑。

## 已完成的修改

### 1. 剪贴板复制修复 (index.html)
HTTP 环境下 `navigator.clipboard.writeText` 不可用。添加了 `_cpy()` 回退函数，使用 `document.execCommand('copy')` 作为替代。所有 `navigator.clipboard.writeText` 调用已替换为 `_cpy()`。

### 2. 用户消息时间戳 (index.html)
在 `_buildUserRow` 末尾添加时间戳显示，格式 MM/DD HH:MM，右对齐，半透明小字。CSS class `.msg-time`。

### 3. Rename 修复 (index.html)
**问题根因**: `closeSessionContextMenu()` 会清空 `sessionContext.session`，但 `openRenameDialog(target)` 没有恢复它，导致 `renameConfirmBtn.onclick` 读到 null 而静默退出。
**修复**: 在 `openRenameDialog` 函数开头加了 `sessionContext.session=target;`。

### 4. 模型配置 (models.json - 仅服务器上)
- 移除了 Codex（不需要）
- 移除了旧模型 (Sonnet 3.5v2, Haiku 3.5, Opus 3)
- 修正了 Opus 4.5 和 Sonnet 4.5 的模型 ID（日期部分）
- 注意: 这些修改目前只在服务器本地，没有提交到 repo

## 待完成的功能

### 1. 模型按窗口独立选择（优先级：高）
**现状**: 模型选择存在 `localStorage` 的 `chat_model` 键，全局共享。切换一个窗口的模型，所有窗口都会变。
**目标**: 每个对话窗口独立记住自己的模型。
**思路**: 将模型绑定到 `conversation_id`，可在前端用 `localStorage` 存 `{conv_id: model}` 映射，或在后端数据库的 conversations 表加 model 字段。

### 2. 绑定域名 + HTTPS
目前通过 IP:端口 直接访问，没有域名也没有 HTTPS。HTTPS 会让 `navigator.clipboard` 等 API 原生可用（不再需要 `_cpy` 回退）。

### 3. 记忆系统整理
- `memories/` 文件夹已清空，等诗整理好 Supabase 里的记忆后重新导入
- 之前导入过 202 条记忆，后来清掉了
- Supabase 表结构: id, date, content, summary, tags, window_name, weight, created_at, photos, embedding
- 导入方式: 用 Python 脚本通过 Supabase REST API 拉取，写成 `{date}_{id:03d}.md` 文件放入 `memories/`

### 4. 为 ChatNest 写 CLAUDE.md（记忆系统）
给 ChatNest 里的 Claude 写 preferences/saved memories，让它知道诗的信息和偏好。

### 5. 替换品牌资源
替换 ChatNest 默认的 logo、图标等，换成我们自己的。

## 记忆系统说明

### 三层结构
1. **Preferences** — 行为指令，每条消息全量注入（上限 50K 字符）
2. **Saved Memories** — 独立事实条目，每条消息全量注入（上限 200 条，每条 4K 字符）
3. **memories/ 文件夹** — RAG 检索，ChromaDB 向量 + jieba/BM25 关键词搜索，top_k=8，budget_chars=2000

### RAG 触发
不需要用户说"你记得xxx"。每条消息都会自动触发 RAG 搜索，从 memories/ 里找相关内容注入上下文。

### Preferences vs Saved Memories 区别
- **Preferences**: 写"你应该怎么做"——语气、称呼、行为规则
- **Saved Memories**: 写"你知道什么"——事实、偏好、经历
- 两者都是全量注入，但语义用途不同

## 关键文件速查

| 文件 | 用途 |
|------|------|
| `full-stack/static/index.html` | 前端（单文件应用，JS/CSS/HTML 全在里面） |
| `full-stack/app/main.py` | FastAPI 后端主文件，所有 API 端点 |
| `full-stack/app/claude.py` | Claude SDK 后端（订阅模式） |
| `full-stack/app/claude_api.py` | Claude API 后端（按量付费模式，未使用） |
| `full-stack/app/store.py` | SQLite 数据层 |
| `full-stack/app/auth.py` | 认证逻辑 |
| `full-stack/app/memory.py` | 记忆管理 |
| `full-stack/models.json` | 可用模型列表 |
| `full-stack/.env` | 环境变量配置（不在 git 里） |
| `full-stack/memories/` | RAG 记忆文件目录 |

## 部署更新流程
在服务器上执行：
```bash
cd ~/chatnest && git pull origin main && sudo systemctl restart chatnest
```
如果 pull 冲突（因为本地有 models.json 等修改），用：
```bash
cd ~/chatnest && git stash && git pull origin main && git stash pop && sudo systemctl restart chatnest
```
如果 stash pop 也冲突（尤其是 index.html），用：
```bash
cd ~/chatnest && git fetch origin main && git checkout origin/main -- full-stack/static/index.html && sudo systemctl restart chatnest
```

## 注意事项
- 修改 `index.html` 时优先在 repo 里改，再 pull 到服务器。避免在服务器上直接编辑导致 git 冲突。
- `.env` 文件不在 git 里，服务器独有。
- `conversations.db` 数据库文件不在 git 里，服务器独有。
- models.json 的模型 ID 日期要和 Anthropic 官方一致，否则会 404。
