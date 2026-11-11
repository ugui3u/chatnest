# 品牌占位符说明

这个公开包有意保持品牌中性。界面里保留了稳定的占位符位置，方便你换成自己拥有或已获授权的素材。

这是占位符版本。要替换占位 logo、图标、状态动画和字体，请从这里下载单独素材包：

https://drive.google.com/drive/folders/1EFaL-cwFn262Mu8L9s-cO9dw6FC4LAMr

然后按下面的位置替换。

## 占位符位置

- `index.html`：`<symbol id="claude-mark">` 是通用圆形占位图标。
- `index.html`：`<symbol id="claude-spinner-mark">` 是通用加载占位图标。
- `index.html`：`.mobile-sidebar-title` 是纯文字，不是专有字标。
- `index.html`：`CLAUDE_LOGO_SPRITES` 使用通用 SVG 状态动画。
- `index.html`：网页标题、输入框占位文案、免责声明、导航 aria-label、资料页占位文案和复制对话前缀都使用中性文案。
- `index.html`：专有或远程品牌字体已移除，CSS 会回退到系统字体。

## 如何替换

把这些占位符替换成你自己拥有或已获授权的 SVG、字体、名称和文案。如果你维护一个私有素材包，请确保里面只放你有权使用的素材；除非你拥有分发权，否则不要把那个素材包一起公开发布。

## 注意

代码里的类名或函数名可能还保留历史供应商词汇，这是为了兼容旧逻辑，属于实现细节。公开版的可见 UI 素材和文案已经改成占位符。
