# 私聊界面使用原生 Rich Messages，通过原始 API 调用实现

管理员私聊里的卡片类消息（status message、confirmation card、菜单页）改用 Telegram Bot API 10.1+ 的原生 Rich Messages 发送和编辑。python-telegram-bot 22.8 只原生支持到 Bot API 10.0，所以这些调用通过 `Bot.do_api_request` 直接发原始方法（`sendRichMessage`、`editMessageText` 的 `rich_message` 参数），自己拼 JSON。

## 考虑过的方案

- 现有 HTML parse mode 加带颜色的 InlineKeyboardButton：22.8 现成可用，不依赖新服务器版本，但没有标题、表格等排版能力。
- 先做 HTML，把渲染集中到一层，等 PTB 原生支持后再切换。

## 后果

- Local Bot API Server 必须是支持 Rich Messages 的版本（10.1+，用到 rich 按钮时 10.3+），部署配置固定为 `aiogram/telegram-bot-api:10.3`。
- rich 发送或编辑失败时退回普通 HTML 消息，所以每种卡片都要同时维护 rich 和 HTML 两种渲染。一条消息从发出到结束保持同一种格式，不在 rich 和普通消息之间互相编辑转换。
- 按钮统一放在 `reply_markup` 的 inline keyboard，不使用 rich 正文内嵌的按钮，这样退回 HTML 时按钮不受影响。
- 调用和返回值都是原始字典，测试要针对请求 JSON 断言，没有 PTB 的类型检查。
- PTB 发布支持 10.1+ 的版本后，要把原始调用迁移成库方法，迁移范围限定在卡片渲染和发送那一层。
