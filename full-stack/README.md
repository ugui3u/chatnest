# AI 聊天完整功能版

这是一个偏移动端体验的个人聊天 Web App，包含模型切换、流式回复、工具状态、思考/工具摘要、上传、对话历史、记忆接口、资料与偏好设置。

这个公开包是品牌中性的：专有 logo、远程品牌字体和私人 prompt 细节都已替换成占位符。

## 占位符说明

很多可见元素都是故意留成占位符：顶部/状态图标、加载图标、侧边栏字标、工具/状态动画、网页标题、输入框占位文案、免责声明和字体。它只是占位符版本。要替换 logo、图标和字体，请从下面链接下载单独素材包，并按 `BRANDING.md` 操作：

https://drive.google.com/drive/folders/1EFaL-cwFn262Mu8L9s-cO9dw6FC4LAMr

## 运行

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp env.example .env
python3 - <<'PY'
import secrets
print('CHAT_SECRET=' + secrets.token_urlsafe(32))
PY
```

编辑 `.env`，设置 `CHAT_PASSWORD` 和 `CHAT_SECRET`，然后启动：

```bash
./run.sh
```

打开 `http://127.0.0.1:8787/`。

## 登录保护

`AUTH_MODE=app` 只使用应用内密码，是推荐默认模式。

`AUTH_MODE=both` 会额外启用浏览器 Basic Auth。只有在 HTTPS 或可信反向代理后面才建议使用。

## 选择 CC 或 API 后端

两种 Claude 后端通过 `.env` 选择，修改后重启 `./run.sh`。默认保持 CC。
不需要修改前端；Codex 模型仍走原来的 Codex CLI，与这个开关独立。

### 方式一：Claude Code（CC）

```dotenv
CHAT_BACKEND=sdk
```

配置 Claude Code / Agent SDK 所需的 CLI、登录和运行环境，先确认在运行
小窝的同一台机器、同一用户下可以使用 Claude Code。此模式保留 CC 的
会话、搜索、文件读写和命令工具。请按 Claude Code 当前支持的认证方式配置。
如果使用订阅登录，不要同时留下其他用途的 API 环境变量，以免认证来源混淆。

### 方式二：直接连接 API

不需要 Claude Code 订阅或登录。在 `.env` 中填写：

```dotenv
CHAT_BACKEND=api
ANTHROPIC_API_KEY=替换成你的密钥
ANTHROPIC_BASE_URL=https://api.anthropic.com
```

第三方服务将 base URL 换为服务商提供的 **Anthropic 兼容基础地址**。
允许末尾带 `/v1`，不要填写完整的 `/v1/messages`。只支持 OpenAI
`/v1/chat/completions` 的接口不能直接使用这条后端。

在 `models.json` 中填写服务商实际支持的模型 ID。`thinking` 可以设置为
`none`、`extended` 或 `adaptive`；不支持思考的服务商应设为 `none`，
或者在界面关闭扩展思考。示例模型列表不代表你的账号一定有使用权限。

API 模式支持流式回复、多轮历史、编辑/重试、Profile 和记忆上下文，
并直接发送上传的 PNG/JPEG/GIF/WebP 图片、PDF（单个不超过 8MB）和
UTF-8 文本（单个不超过 200KB）。模型也必须支持对应附件类型；长历史和
附件会增加 token 消耗，超出服务商上下文限制时请新建对话。

**API 模式当前不执行 CC 的搜索、文件读写、命令或自动保存记忆工具。**
Profile 可继续在界面手动维护，本地记忆检索可继续启用。
历史思考仅供界面展示，不会以缺少签名的 thinking 块回传 API。

思考小标题默认关闭，不会偷偷调用 CC。如果需要，另设
`API_SUMMARY_MODEL=服务商支持的模型ID`，此功能会产生额外 API 请求和费用。
在两个后端之间切换后，下一轮会延续数据库中可见的聊天文字，
不会把 API 的会话 ID 当作 CC 会话恢复。

### 连接排查

- 先确认在 `full-stack/` 下运行，并在修改 `.env` 后重启后端。
- 401/403：检查 key、服务商权限和访问限制。
- 404：检查基础地址、接口协议和模型 ID。
- 400：检查模型、思考参数和附件支持；可先关闭扩展思考测试。
- 连接失败/超时：检查**运行后端的机器**能否访问服务商。
  如需代理，使用该机器实际可达的 `HTTP_PROXY` / `HTTPS_PROXY` 地址。
  容器里的 `127.0.0.1` 指向容器自身，VPS 上的它指向 VPS。

`8787` 是小窝网页服务端口，`3900` 是本地记忆服务端口，它们都不是
模型 API 或代理端口。使用 API 不等于一定不需要代理。

### 本地回归验证（不消耗 API 额度）

安装运行依赖和测试用 HTTP 客户端后执行：

```bash
pip install httpx
python3 -m unittest discover -s tests -v
```

测试使用隔离的临时数据库和模拟 API，覆盖聊天后端路由、多轮历史、
编辑/重试、流式输出、附件及错误提示。真实供应商仍需使用自己的 key 验证。

## 记忆检索

完整功能版包含两层记忆能力：

- Saved memories / Preferences：保存在本地 `profile.json`，会直接注入上下文。
- 本地记忆检索服务：使用 ChromaDB 向量检索 + jieba/BM25 关键词检索，从 `CLAUDE.md`、`profile.json` 和 `memories/` 里的文本中检索相关片段。

只启动主应用：

```bash
./run.sh
```

同时启动主应用和本地记忆检索：

```bash
./run-with-memory.sh
```

也可以分两个终端运行：

```bash
./run-memory-vectorize.sh
./run-memory-search.sh
./run.sh
```

这个检索服务默认监听 `http://127.0.0.1:3900/search`，只在本机使用。它不包含你的真实记忆数据、向量数据库或索引状态；用户需要自己创建 `CLAUDE.md`、`profile.json` 或 `memories/`，再运行 `./run-memory-vectorize.sh` 生成自己的本地索引。

## 不包含什么

公开包不包含 `.env`、对话数据库、上传文件、记忆库、向量数据库、日志、私人文档、专有字体或私有品牌素材。

## 许可证

非商用使用。允许非商业复制、修改和再发布；禁止商业使用；署名不强制。
