# 领域文档

说明这套 skill 在探索代码库时如何读取和使用领域文档。

## 探索之前先读这些

- 仓库根的 **`GLOSSARY.md`**，或者
- 仓库根的 **`GLOSSARY-MAP.md`**（如果它存在）：它指向每个上下文各自的 `GLOSSARY.md`，
  **和当前话题相关的都读一遍**。
- **`docs/adr/`**：读那些涉及你即将动手的区域的 ADR。
  多上下文仓库里，还要看 `src/<context>/docs/adr/` 下**该上下文范围内**的决定。

这些文件不存在时直接继续，不提示缺失，也不主动建议创建。
`domain-modeling` skill（经由 `grill-demand` 与 `improve-codebase-architecture` 到达）
会在术语或决定确认后按需创建它们。

## 文件结构

单上下文仓库（绝大多数）：

```text
/
├── GLOSSARY.md
├── docs/adr/
│   ├── 0001-event-sourced-orders.md
│   └── 0002-postgres-for-write-model.md
└── src/
```

多上下文仓库（根目录存在 `GLOSSARY-MAP.md`）：

```text
/
├── GLOSSARY-MAP.md
├── docs/adr/                          ← 系统级决策
└── src/
    ├── ordering/
    │   ├── GLOSSARY.md
    │   └── docs/adr/                  ← 该上下文自己的决策
    └── billing/
        ├── GLOSSARY.md
        └── docs/adr/
```

## 使用术语表里的词汇

**当你的产出点到一个领域概念时**（issue 标题、重构提案、假设、测试名），
**用 `GLOSSARY.md` 里定义的那个词**。不要滑向术语表明确要避免的同义词。

**如果你需要的概念还不在术语表里，这是个信号**：要么你在发明这个项目不用的语言（重新考虑），
要么这里真有一个缺口（**记下来交给 `domain-modeling`**）。

## 标出与 ADR 的冲突

**如果你的产出与某条现有 ADR 相抵触，就把它明确摆出来**，而不是悄悄覆盖掉：

> *与 ADR-0007（event-sourced orders）相抵触，但值得重开，因为……*
