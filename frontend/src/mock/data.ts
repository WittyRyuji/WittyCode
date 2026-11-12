import type {
  ActivityBlock,
  ApiProvider,
  AttachmentView,
  Block,
  ChatGPTAccount,
  DevinAccount,
  Effort,
  Engine,
  ModelChoice,
  PermissionPrompt,
  Project,
  QuestionPrompt,
  QueuedMessage,
  Session,
  ToolItem,
  Turn,
} from "../types";

/** The models of a Devin account, run by `devin acp`. Ids carry a `devin:` prefix; the levels are the model's own. */
export const DEVIN_MODELS: ModelChoice[] = [
  { id: "devin:swe-2-high", label: "SWE-2", note: "Free", source: "devin", group: "Cognition", efforts: ["medium", "high", "max"], defaultEffort: "high", window: 262_000 },
  { id: "devin:claude-opus-5-5-medium", label: "Claude Opus 5.5", source: "devin", group: "Anthropic", efforts: ["low", "medium", "high", "xhigh", "max"], defaultEffort: "medium", window: 1_000_000 },
  { id: "devin:gpt-6-sol-medium", label: "GPT-6 Sol", source: "devin", group: "OpenAI", efforts: ["none", "low", "medium", "high", "xhigh", "max"], defaultEffort: "medium", window: 1_000_000 },
  { id: "devin:glm-5-2", label: "GLM-5.2 High", source: "devin", group: "Zhipu", efforts: ["none", "high", "max"], defaultEffort: "high", window: 200_000, noImages: true },
];

/** The ChatGPT plan run by WittyCode's own harness. Ids carry a `chatgpt:` prefix; the figures come from the account's own list. */
export const CHATGPT_MODELS: ModelChoice[] = [
  { id: "chatgpt:gpt-6.1-sol", label: "GPT-6.1-Sol", source: "chatgpt", efforts: ["low", "medium", "high", "xhigh"], defaultEffort: "medium", window: 400_000 },
  { id: "chatgpt:gpt-6-astra", label: "GPT-6-Astra", source: "chatgpt", efforts: ["low", "medium", "high", "xhigh"], defaultEffort: "medium", window: 400_000 },
  { id: "chatgpt:gpt-6-luna", label: "GPT-6-Luna", note: "快", source: "chatgpt", efforts: ["low", "medium", "high"], defaultEffort: "low", window: 400_000 },
];

/** A model entry of an API provider as the model menu needs it. Unknown models have a reasoning control only when the
 *  entry sets `reasoningEffort` (the menu then offers Off / Low / Medium / High); a known `baseModelId` would bring its own. */
export function modelsOfProvider(p: ApiProvider): ModelChoice[] {
  return p.models.map((m) => ({
    id: `api:${p.id}:${m.id}`,
    label: m.displayName || m.id,
    source: `api:${p.id}`,
    efforts: m.reasoning ? (["off", "low", "medium", "high"] as Effort[]) : [],
    defaultEffort: m.reasoning,
    window: m.contextWindow ?? 250_000,
  }));
}


export const DEVIN_ACCOUNT: DevinAccount = {
  cli: "found",
  cliPath: "C:\\Users\\me\\AppData\\Local\\Programs\\devin\\resources\\app\\extensions\\windsurf\\devin\\bin\\devin.exe",
  version: "3000.11.238",
  auth: "signed-in",
  email: "demo@example.com",
  plan: "Devin Pro",
  enabled: true,
};

export const CHATGPT_ACCOUNT: ChatGPTAccount = {
  auth: "signed-in",
  email: "demo@example.com",
  planEnabled: true,
  clientId: "oaiapp_demo0001",
  accounts: [{ clientId: "oaiapp_demo0001", label: "demo@example.com", signedIn: true, planEnabled: true, active: true }],
  enabled: true,
  welcomed: true,
  available: true,
};

export const API_PROVIDERS: ApiProvider[] = [
  {
    id: "openrouter",
    name: "OpenRouter",
    kind: "generic-chat-completion-api",
    baseUrl: "https://openrouter.ai/api/v1",
    keyMode: "env",
    keyHint: "${OPENROUTER_API_KEY}",
    models: [
      { id: "openai/gpt-oss-120b", displayName: "GPT-OSS 120B", contextWindow: 131_072, maxOutput: 16_384, reasoning: "medium", images: false },
      { id: "qwen/qwen3-coder", displayName: "Qwen3 Coder", contextWindow: 262_144, maxOutput: 16_384, reasoning: "", images: false },
    ],
    headers: [{ k: "HTTP-Referer", v: "https://example.com" }],
    extraArgs: "",
    status: { ok: true, text: "连接成功 · 380 ms · 3 个模型" },
  },
  {
    id: "anthropic-direct",
    name: "Anthropic（自己的 Key）",
    kind: "anthropic",
    baseUrl: "https://api.anthropic.com",
    keyMode: "inline",
    keyHint: "sk-ant-••••••••7Qd2",
    models: [{ id: "claude-sonnet-4-5-20250929", displayName: "Sonnet 4.5（直连）", contextWindow: 200_000, maxOutput: 32_000, reasoning: "medium", images: true }],
    headers: [],
    extraArgs: "",
  },
  {
    id: "ollama",
    name: "Ollama（本机）",
    kind: "generic-chat-completion-api",
    baseUrl: "http://localhost:11434/v1",
    keyMode: "none",
    keyHint: "",
    models: [{ id: "qwen3:30b", displayName: "Qwen3 30B", contextWindow: 32_768, maxOutput: 8_192, reasoning: "", images: false }],
    headers: [],
    extraArgs: "",
    status: { ok: false, text: "连不上 localhost:11434：Ollama 没有运行？" },
  },
];

/** Whether a turn stops for the user's approval at this permission level (the simulator only). */
export const asksFirst = (engine: Engine, level: string) =>
  engine === "devin" ? level === "code" || level === "plan" : level !== "full";

const WINDOWS: Record<string, { window: number }> = Object.fromEntries(
  [...DEVIN_MODELS, ...CHATGPT_MODELS, ...API_PROVIDERS.flatMap(modelsOfProvider)].map((m) => [m.id, { window: m.window }]),
);

// ------------------------------------------------------------------------------------------------ small builders

let counter = 0;
const nid = (p: string) => `${p}-${++counter}`;

function tool(
  name: string,
  category: ToolItem["category"],
  description: string,
  extra: Partial<ToolItem> = {},
): ToolItem {
  return { kind: "tool", id: nid("t"), name, category, description, status: "done", ...extra };
}

function group(...entries: ActivityBlock["entries"]): ActivityBlock {
  return { kind: "activity", id: nid("g"), entries };
}

const text = (markdown: string, streaming = false): Block => ({ kind: "text", id: nid("x"), markdown, streaming });
const user = (t: string, attachments?: AttachmentView[]): Block => ({ kind: "user", id: nid("u"), text: t, attachments });

function turn(blocks: Block[], extra: Partial<Turn> = {}): Turn {
  return { id: nid("turn"), blocks, prompts: [], status: "done", ...extra };
}

// ------------------------------------------------------------------------------------------------ shared pieces

export const HELLO_NEW_DIFF = {
  path: "C:\\Users\\me\\code\\wittycode\\sandbox\\demo\\hello.txt",
  kind: "new" as const,
  added: 1,
  removed: 0,
  lines: [{ t: "add" as const, new: 1, text: "hi" }],
};

export const CONTEXT_DIFF = {
  path: "desktop/core/context.py",
  kind: "edit" as const,
  added: 9,
  removed: 2,
  lines: [
    { t: "gap" as const, text: "第 38 行之前省略" },
    { t: "ctx" as const, old: 38, new: 38, text: "    state = ContextState(window=_tokens(stats.limit))" },
    { t: "ctx" as const, old: 39, new: 39, text: "    client = getattr(session, \"_require_client\", None)" },
    { t: "del" as const, old: 40, text: "    breakdown = await client().get_context_breakdown()" },
    { t: "del" as const, old: 41, text: "    used = _tokens(breakdown.last_call_compaction_tokens)" },
    { t: "add" as const, new: 40, text: "    breakdown = await client().get_context_breakdown()" },
    { t: "add" as const, new: 41, text: "    fixed = _fixed_part(breakdown)" },
    { t: "add" as const, new: 42, text: "    used = _tokens(breakdown.last_call_compaction_tokens)" },
    { t: "add" as const, new: 43, text: "    if used is not None and not plausible(used, fixed):" },
    { t: "add" as const, new: 44, text: "        used = None  # a side call such as the session title" },
    { t: "ctx" as const, old: 42, new: 45, text: "    return replace(state, used=used, fixed=fixed)" },
    { t: "gap" as const, text: "后面 63 行省略" },
  ],
};

const REVIEW_MARKDOWN = `## 结论

两个分支的实现是**忠实移植**，但有 4 处结构性问题，合起来解释了约 10 倍的慢：

1. **每行一次提交**：导入 N 行就开 N 个事务，提交次数 = 行数。
2. **两张表共用一个连接，串行写入**，等待互相叠加。
3. **每行都重新编译一遍校验用的正则**。
4. **日志同步写盘**，每行一条，把 I/O 占满。

### 估计影响

| # | 问题 | 位置 | 预计影响 |
|---|------|------|----------|
| 1 | 每行一次提交 | \`importer.py:605\` | 高 |
| 2 | 共用连接、串行写入 | \`writer.py:354\` | 高 |
| 3 | 每行编译正则 | \`validate.py:368\` | 中 |
| 4 | 日志同步写盘 | \`log.py:119\` | 中 |

### 参考实现的做法

参考实现按 **1000 行一批**提交，并给队列设了 5 万行的上限：

\`\`\`python
# reference: 攒满一批再提交
batch.append(row)
if len(batch) >= BATCH_SIZE:  # 1000
    conn.executemany(INSERT, batch)
    conn.commit()
    batch.clear()
\`\`\`

> 建议先做第 1、2 项：改动小，而且最可能一次拿回大部分性能。

### 先验证再改

\`\`\`powershell
$env:IMPORT_PROFILE = 1
.\\import.exe --stats
\`\`\`

可以把批大小先设为 **1000**、并发写入数设为 **1** 做对照，看耗时是否下降。详见 [参考实现](https://example.com/bulk-import)。`;

const IMAGE_SAMPLE: AttachmentView = {
  id: "att-img",
  kind: "image",
  name: "粘贴的图片.png",
  size: 10_900,
  url:
    "data:image/svg+xml;utf8," +
    encodeURIComponent(
      `<svg xmlns='http://www.w3.org/2000/svg' width='320' height='180'><rect width='320' height='180' fill='#fdf3b0'/><rect x='24' y='34' width='96' height='96' fill='#e5484d'/><circle cx='190' cy='82' r='46' fill='#2f6bff'/><polygon points='250,130 290,50 300,130' fill='#16a34a'/><text x='24' y='162' font-family='Arial' font-weight='700' font-size='16' fill='#222'>TANGERINE-4217</text></svg>`,
    ),
  width: 320,
  height: 180,
};
const TXT_SAMPLE: AttachmentView = { id: "att-txt", kind: "text", name: "notes.txt", size: 89 };
const PDF_SAMPLE: AttachmentView = { id: "att-pdf", kind: "pdf", name: "invoice.pdf", size: 697 };

/** Messages waiting behind a running turn, for `?queued=N` (the design page's way to show the strip above the composer). */
export const QUEUED_SAMPLE: QueuedMessage[] = [
  { id: "q1", text: "然后把测试也补上，顺便检查一下 lint 有没有新增的警告，最后总结一下这次改动的文件和每个文件改了什么、为什么这么改", attachments: [] },
  { id: "q2", text: "再跑一遍完整测试", attachments: [PDF_SAMPLE] },
];
// ------------------------------------------------------------------------------------------------ sessions

const baseUsage = (n: number) => ({ input: 394 * n, output: 498 * n, cacheWrite: 3_207 * n, cacheRead: 68_300 * n });

function settings(model = "chatgpt:gpt-6.1-sol", autonomy = "code", reasoning: Effort | "" = "low") {
  return { model, reasoning, autonomy, mode: "auto" };
}

function ctx(model: string, used: number | null, compacted = false) {
  return { used, model, compacted, ...WINDOWS[model]! };
}

function session(p: Partial<Session> & Pick<Session, "id" | "projectId" | "title">): Session {
  const model = p.settings?.model ?? "chatgpt:gpt-6.1-sol";
  return {
    modified: "刚刚",
    turns: [],
    usage: null,
    context: ctx(model, null),
    settings: settings(model),
    workingLabel: "就绪",
    draft: "",
    attachments: [],
    ...p,
  };
}

const fileOps: Session = session({
  id: "s-file-ops",
  projectId: "p-wittycode",
  title: "File operations task",
  speed: { tps: 42.3, tokens: 1_840, seconds: 43.5, first: 1.4, genTps: 43.7 },
  mark: "ready",
  usage: baseUsage(1),
  context: ctx("chatgpt:gpt-6.1-sol", 18_000),
  turns: [
    turn(
      [
        user(
          "List the files here, read README.txt, then create hello.txt containing 'hi', then edit hello.txt so it says 'hi there'. Finish with a short two-line summary of what you did.",
        ),
        group(
          tool("LS", "read", "C:\\Users\\me\\code\\wittycode\\sandbox\\demo", {
            meta: { directory_path: "C:\\Users\\me\\code\\wittycode\\sandbox\\demo" },
            output: "README.txt    222 B",
            seconds: 0.1,
          }),
          tool("Read", "read", "README.txt", {
            meta: { file_path: "C:\\Users\\me\\code\\wittycode\\sandbox\\demo\\README.txt" },
            output:
              "This folder is a throwaway sandbox for WittyCode live checks.\nEverything in here may be created, changed, or deleted by those turns.",
            seconds: 0.1,
          }),
        ),
        text(
          'The folder holds only README.txt, which says the folder is disposable. Next I\'ll create hello.txt and change it to "hi there".',
        ),
        group(
          tool("Create", "edit", "hello.txt", { diffs: [HELLO_NEW_DIFF], decision: "proceed_once", seconds: 2.4 }),
          tool("Edit", "edit", "hello.txt", {
            diffs: [
              {
                path: "hello.txt",
                kind: "edit",
                added: 1,
                removed: 1,
                lines: [
                  { t: "del", old: 1, text: "hi" },
                  { t: "add", new: 1, text: "hi there" },
                ],
              },
            ],
            decision: "proceed_once",
            seconds: 1.8,
          }),
        ),
        text(
          "I listed the folder, which contains only `README.txt`. The README says the folder is a throwaway space for a live check.\n\nI created `hello.txt` with \"hi\" and then edited it to say \"hi there\". The notes are in `docs/handoff.md:3-8`, and [the design](docs/design.md) has the rest.",
        ),
      ],
      { seconds: 12, steps: 4 },
    ),
  ],
});

const liveRefactor: Session = session({
  id: "s-live",
  projectId: "p-wittycode",
  title: "重构 session_manager 的上下文读取",
  mark: "running",
  modified: "刚刚",
  usage: baseUsage(3),
  workingLabel: "正在运行：编辑 desktop/core/context.py",
  context: ctx("chatgpt:gpt-6.1-sol", 41_200),
  settings: settings("chatgpt:gpt-6-astra", "code", "high"),
  turns: [
    turn(
      [
        user("状态栏的「已用」第一轮后显示 357，应该是 22K 左右。找出原因并修好。"),
        group(
          { kind: "thinking", id: nid("th"), complete: true, seconds: 3.2, text: "357 这个数太小了，不像一次对话调用……会话标题是另一次小调用？先看 context.py 怎么取这个数。" },
          tool("Grep", "read", "last_call_compaction_tokens  desktop/", {
            meta: { pattern: "last_call_compaction_tokens", path: "desktop/" },
            output: "desktop/core/context.py:50\ndesktop/core/session_manager.py:783",
            seconds: 0.2,
          }),
          tool("Read", "read", "desktop/core/context.py", {
            meta: { file_path: "desktop/core/context.py" },
            output: "(73 行)",
            seconds: 0.1,
          }),
        ),
        text(
          "找到了：引擎记的 `last_call_compaction_tokens` 是**最后结束的那次模型调用**，而会话标题由另一次约 360 token 的小调用生成，常常比第一轮晚结束。",
        ),
        group(
          tool("TodoWrite", "plan", "3 项", {
            todos: [
              { text: "从 session_token_usage_changed 通知里取每次调用的数字", state: "completed" },
              { text: "用固定部分的一半做下限，过滤旁路调用", state: "in_progress" },
              { text: "补测试并做变异检查", state: "pending" },
            ],
            seconds: 0.1,
          }),
          tool("Edit", "edit", "desktop/core/context.py", {
            status: "running",
            meta: { file_path: "desktop/core/context.py" },
          }),
        ),
        text("下限用固定部分（系统提示、工具、AGENTS.md、技能）的一半：实机对话调用都在它之上，旁路调用只有几百 token", true),
      ],
      { status: "running", steps: 3 },
    ),
  ],
});

const awaitingEdit: PermissionPrompt = {
  kind: "permission",
  id: nid("perm"),
  status: "pending",
  actions: [
    {
      title: "编辑文件 desktop/core/context.py",
      toolName: "Edit",
      path: "desktop/core/context.py",
      impact: "medium",
      diffs: [CONTEXT_DIFF],
    },
  ],
  options: [
    { value: "proceed_once", label: "允许（仅此一次）", tone: "primary" },
    { value: "proceed_always", label: "允许，并始终允许低风险操作", tone: "normal" },
    { value: "cancel", label: "拒绝", tone: "danger" },
  ],
};

const approvalSession: Session = session({
  id: "s-approval",
  projectId: "p-inventory",
  title: "订单库存查询改批量",
  mark: "attention",
  modified: "刚刚",
  workingLabel: "等待你的确认",
  usage: baseUsage(2),
  context: ctx("chatgpt:gpt-6.1-sol", 96_400),
  settings: settings("chatgpt:gpt-6.1-sol", "ask", "max"),
  turns: [
    {
      id: nid("turn"),
      status: "running",
      steps: 3,
      prompts: [awaitingEdit],
      blocks: [
        user("把 order_service.py 里逐条查库存的循环改成一次批量查询，循环里只累计数量。"),
        group(
          tool("Grep", "read", "get_stock  src/inventory/", {
            meta: { pattern: "get_stock", path: "src/inventory/" },
            output: "order_service.py:68\nreport_service.py:41\nstock_repo.py:212",
            seconds: 0.3,
          }),
          tool("Read", "read", "order_service.py", { meta: { file_path: "src/inventory/order_service.py" }, output: "(412 行)", seconds: 0.1 }),
          tool("Edit", "edit", "order_service.py", {
            status: "awaiting_permission",
            diffs: [
              {
                path: "src/inventory/order_service.py",
                kind: "edit",
                added: 4,
                removed: 6,
                lines: [
                  { t: "ctx", old: 66, new: 66, text: "    if line.quantity <= 0:" },
                  { t: "del", old: 67, text: "        stock = repo.get_stock(line.sku)" },
                  { t: "del", old: 68, text: "        available = stock.on_hand - stock.reserved" },
                  { t: "add", new: 67, text: "        wanted[line.sku] += line.quantity" },
                  { t: "add", new: 68, text: "        skus.append(line.sku)  # stock is read once, after the loop" },
                ],
              },
            ],
          }),
        ),
        text("循环里只记下商品编号，库存留到循环后一次批量查询。我要修改 `order_service.py`，请确认："),
      ],
    },
  ],
});

const deadSession: Session = session({
  id: "s-dead",
  projectId: "p-inventory",
  title: "feature 分支代码审查",
  mark: "dead",
  modified: "5 分钟前",
  workingLabel: "连接已断开",
  usage: baseUsage(1),
  context: ctx("chatgpt:gpt-6.1-sol", 52_900),
  settings: settings("chatgpt:gpt-6.1-sol"),
  turns: [
    turn(
      [
        user("继续审查 a1b2c3d 的批量写入，重点看每次请求重建连接池的开销。"),
        group(
          tool("Read", "read", "pool.py", { meta: { file_path: "src/inventory/pool.py" }, output: "(1204 行)", seconds: 0.2 }),
          tool("Execute", "command", "git show a1b2c3d --stat", {
            command: "git show a1b2c3d --stat",
            output: " src/inventory/batch_writer.py | 212 +++++++\n 3 files changed, 301 insertions(+), 12 deletions(-)",
            seconds: 0.9,
          }),
        ),
        text("每次请求都在重建连接池，而且用的是 `pool_size=1`，这在"),
        { kind: "error", id: nid("err"), message: "The engine process exited unexpectedly (exit code 1)", recoverable: true },
      ],
      { status: "failed", seconds: 31, steps: 2 },
    ),
  ],
});

const reviewSession: Session = session({
  id: "s-review",
  projectId: "p-inventory",
  title: "批量导入性能分析",
  mark: "ready",
  modified: "12 分钟前",
  usage: baseUsage(4),
  context: ctx("chatgpt:gpt-6.1-sol", 128_600),
  settings: settings("chatgpt:gpt-6.1-sol", "ask", "xhigh"),
  turns: [
    turn(
      [
        user(
          "对照参考实现，看看这 4 个提交为什么慢了 10 倍。附上我的 profile 截图、笔记和账单。",
          [IMAGE_SAMPLE, TXT_SAMPLE, PDF_SAMPLE],
        ),
        group(
          tool("Execute", "command", "git diff 3c9a1e0..8b2d4f7 --stat", {
            command: "git diff 3c9a1e0..8b2d4f7 --stat",
            output: " 14 files changed, 3512 insertions(+), 187 deletions(-)",
            seconds: 1.2,
          }),
          tool("Read", "read", "importer.py", { meta: { file_path: "importer.py" }, output: "(412 行)", seconds: 0.1 }),
          tool("Read", "read", "reference/bulk_import.py", { meta: { file_path: "bulk_import.py" }, output: "(530 行)", seconds: 0.1 }),
          tool("WebSearch", "other", "bulk insert batch size", {
            meta: { query: "bulk insert batch size" },
            output: "3 条结果",
            seconds: 2.1,
          }),
        ),
        text(REVIEW_MARKDOWN),
        { kind: "qa", id: nid("qa"), pairs: [["先改哪一项？", "第 1、2 项（批量提交和独立连接）"]] } as Block,
        text("好的，那我从**批量提交**开始：先把逐行提交改成每 1000 行一批，再给两张表各配一个独立连接。"),
      ],
      { seconds: 94, steps: 4 },
    ),
  ],
});

const emptySession: Session = session({
  id: "s-empty",
  projectId: "p-demo",
  title: "新会话",
  mark: "ready",
  modified: "刚刚",
});

const questionPrompt: QuestionPrompt = {
  kind: "question",
  id: nid("q"),
  status: "pending",
  questions: [
    {
      topic: "方案",
      question: "界面层用哪种方案？",
      options: ["只把对话区换成 React", "整个界面都用 React（Python 只做后端）", "Electron + Python 后台"],
      multiSelect: false,
    },
    {
      topic: "迁移",
      question: "迁移期间要保留哪些旧功能的对照测试？",
      options: ["附件（粘贴、拖放、预览）", "审批和提问", "会话恢复和历史", "上下文用量显示"],
      multiSelect: true,
    },
  ],
};

const questionSession: Session = session({
  id: "s-question",
  projectId: "p-wittycode",
  title: "React 迁移方案",
  mark: "attention",
  modified: "刚刚",
  workingLabel: "等待你的确认",
  usage: baseUsage(1),
  context: ctx("chatgpt:gpt-6.1-sol", 37_800),
  settings: settings("chatgpt:gpt-6.1-sol"),
  turns: [
    {
      id: nid("turn"),
      status: "running",
      steps: 1,
      prompts: [questionPrompt],
      blocks: [
        user("我想把整个界面换成 React，先帮我把方案定下来。"),
        group(tool("AskUser", "ask", "界面层用哪种方案？ 等 2 个问题", { status: "awaiting_permission" })),
        text("开始之前有两件事需要你定："),
      ],
    },
  ],
});

/** A second ChatGPT Plan session: another model of the plan, its own token figures (no cache writes). */
const indexSession: Session = session({
  id: "s-index",
  projectId: "p-inventory",
  title: "重写索引构建",
  mark: "ready",
  modified: "8 分钟前",
  usage: { input: 5_210, output: 3_880, cacheWrite: null, cacheRead: 61_400 },
  speed: { tps: 63.4, tokens: 3_880, seconds: 61.2, first: 2.1, genTps: 65.6 },
  context: ctx("chatgpt:gpt-6-astra", 38_400),
  settings: settings("chatgpt:gpt-6-astra", "code", "high"),
  turns: [
    turn(
      [
        user("把 index_builder.py 里每次都全量重建索引的部分改成只在记录数变化超过 10% 时重建，其余只增量更新。"),
        group(
          tool("Execute", "command", "rg -n \"build_index|refresh\" src/inventory", {
            command: "rg -n \"build_index|refresh\" src/inventory",
            output: "src/inventory/index_builder.py:88:  build_index(...)\nsrc/inventory/service.py:761:  refresh_index()",
            seconds: 0.6,
          }),
          tool("ApplyPatch", "edit", "src/inventory/index_builder.py", {
            decision: "proceed_once",
            seconds: 1.4,
            diffs: [
              {
                path: "src/inventory/index_builder.py",
                kind: "edit",
                added: 6,
                removed: 1,
                lines: [
                  { t: "ctx", old: 86, new: 86, text: "def update(self, record_count: int) -> None:" },
                  { t: "del", old: 87, text: "    self.rebuild(record_count)" },
                  { t: "add", new: 87, text: "    change = abs(record_count - self._last_built) / max(1, self._last_built)" },
                  { t: "add", new: 88, text: "    if change > 0.10:" },
                  { t: "add", new: 89, text: "        self.rebuild(record_count)  # the layout no longer fits" },
                  { t: "add", new: 90, text: "    else:" },
                  { t: "add", new: 91, text: "        self.refresh(record_count)  # same layout, new entries" },
                ],
              },
            ],
          }),
        ),
        text("改好了：记录数相对上次构建变化超过 10% 才重建索引，其余只做 `refresh`。我没有运行，下一步可以在 `index_builder.py` 加一个统计，看看实际多少次走了 refresh。"),
      ],
      { seconds: 41, steps: 2 },
    ),
  ],
});

/** A session run by Devin: its own models (the thought levels differ per model), permission modes and token figures. */
const devinSession: Session = session({
  id: "s-devin",
  projectId: "p-inventory",
  title: "Devin：给配置参数加范围检查",
  mark: "ready",
  modified: "3 分钟前",
  usage: { input: 8_816, output: 145, cacheWrite: null, cacheRead: 5_632 },
  speed: { tps: 38.7, tokens: 145, seconds: 3.7, first: 1.6, genTps: 69.0 },
  context: ctx("devin:swe-2-high", 14_800),
  settings: settings("devin:swe-2-high", "code", "high"),
  turns: [
    turn(
      [
        user("给导入的 batch_size 参数加一个范围检查，超出 [1, 10000] 就报错。"),
        group(
          tool("Read", "read", "import_params.py", { seconds: 0.2 }),
          tool("Edit", "edit", "import_params.py", {
            seconds: 0.9,
            diffs: [
              {
                path: "import_params.py",
                kind: "edit",
                added: 2,
                removed: 0,
                lines: [
                  { t: "ctx", old: 12, new: 12, text: "def set_batch_size(value: int) -> None:" },
                  { t: "add", new: 13, text: "    if not 1 <= value <= 10000:" },
                  { t: "add", new: 14, text: "        raise ValueError(f\"batch_size {value} is outside [1, 10000]\")" },
                ],
              },
            ],
          }),
        ),
        text("加好了：`set_batch_size` 现在会拒绝 [1, 10000] 之外的值，并在错误里写出收到的数。没有改调用方。"),
      ],
      { seconds: 9, steps: 2 },
    ),
  ],
});

/** A session run by WittyCode's own harness on the user's ChatGPT plan: the "Using ChatGPT plan" note beside the controls,
 *  WittyCode's own three permission levels, and the tools it runs itself. */
const chatgptSession: Session = session({
  id: "s-chatgpt",
  projectId: "p-inventory",
  title: "ChatGPT Plan：核对命中率的口径",
  mark: "ready",
  modified: "刚刚",
  usage: { input: 6_420, output: 310, cacheWrite: null, cacheRead: 4_096 },
  speed: { tps: 86.2, tokens: 310, seconds: 3.6, first: 0.9, genTps: 114.8 },
  context: ctx("chatgpt:gpt-6.1-sol", 11_900),
  settings: settings("chatgpt:gpt-6.1-sol", "code", "medium"),
  turns: [
    turn(
      [
        user("命中率那段的口径对吗？看一下 stats.py。"),
        group(
          tool("Grep", "read", "“hit_rate”", { seconds: 0.2 }),
          tool("Read", "read", "stats.py", { seconds: 0.1 }),
          tool("Execute", "command", "python -m pytest tests/test_stats.py -q", { seconds: 2.4 }),
        ),
        text("口径有一处不一致：`hit_rate` 把「缓存读取」算进了分母，而文档里写的是只算新输入。测试通过是因为样例里缓存读取为 0。要我改成文档的口径并补一条测试吗？"),
      ],
      { seconds: 11, steps: 3 },
    ),
  ],
});

/** A session on an API provider's model that has no reasoning control (the reasoning chip is greyed out). */
const apiSession: Session = session({
  id: "s-api",
  projectId: "p-demo",
  title: "用 Qwen3 Coder 整理笔记",
  mark: "ready",
  modified: "刚刚",
  usage: { input: 2_130, output: 960, cacheWrite: 0, cacheRead: 0 },
  speed: { tps: 27.5, tokens: 960, seconds: 34.9, first: 4.8, genTps: 31.9 },
  context: ctx("api:openrouter:qwen/qwen3-coder", 3_100),
  settings: settings("api:openrouter:qwen/qwen3-coder", "ask", ""),
  turns: [
    turn(
      [
        user("把 notes.txt 里的待办整理成一个清单。"),
        text("好的，已整理：\n\n- [ ] 核对命中率的口径\n- [ ] 确认密钥只放环境变量\n- [ ] 接入 Devin 登录"),
      ],
      { seconds: 6.2, steps: 0 },
    ),
  ],
});

const saved = (id: string, projectId: string, title: string, modified: string): Session =>
  session({ id, projectId, title, modified, mark: undefined });

export const SESSIONS: Session[] = [
  fileOps,
  liveRefactor,
  questionSession,
  saved("s-flicker", "p-wittycode", "修复点击闪烁", "1 小时前"),
  saved("s-attach", "p-wittycode", "附件功能设计", "昨天"),
  saved("s-rename", "p-wittycode", "改名为 WittyCode", "3 天前"),
  reviewSession,
  approvalSession,
  indexSession,
  devinSession,
  chatgptSession,
  deadSession,
  saved("s-batch", "p-inventory", "批大小调优", "2 天前"),
  emptySession,
  apiSession,
];

export const PROJECTS: Project[] = [
  {
    id: "p-wittycode",
    name: "wittycode",
    path: "C:\\Users\\me\\code\\wittycode",
    collapsed: false,
    sessionIds: ["s-file-ops", "s-live", "s-question", "s-flicker", "s-attach", "s-rename"],
  },
  {
    id: "p-inventory",
    name: "inventory-api",
    path: "C:\\Users\\me\\code\\inventory-api",
    collapsed: false,
    sessionIds: ["s-review", "s-approval", "s-index", "s-devin", "s-chatgpt", "s-dead", "s-batch"],
  },
  {
    id: "p-demo",
    name: "demo",
    path: "C:\\Users\\me\\code\\wittycode\\sandbox\\demo",
    collapsed: false,
    sessionIds: ["s-empty", "s-api"],
  },
];

export const OPEN_TABS = ["s-file-ops", "s-live", "s-question", "s-review", "s-approval", "s-index", "s-dead", "s-empty", "s-api", "s-devin", "s-chatgpt"];

/** The canned answer the simulator streams when you send a message. */
export const SIM_ANSWER = `我看了一下，改动分两步：

1. 在 \`context.py\` 里加 \`plausible()\`，用固定部分的一半做下限；
2. 在 \`_on_notification\` 里直接取通知自带的数字。

\`\`\`python
def plausible(used: int, fixed: int | None) -> bool:
    return fixed is None or used * 2 >= fixed
\`\`\`

两处都已经有测试覆盖，需要我继续补变异检查吗？`;
