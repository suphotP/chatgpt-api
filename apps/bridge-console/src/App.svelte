<script lang="ts">
  import { onMount } from "svelte";
  import AccountList from "./lib/AccountList.svelte";
  import ApiFieldGuide from "./lib/ApiFieldGuide.svelte";
  import Badge from "./lib/Badge.svelte";
  import CaptureResult from "./lib/CaptureResult.svelte";
  import CodeBlock from "./lib/CodeBlock.svelte";
  import ImageResult from "./lib/ImageResult.svelte";
  import Input from "./lib/Input.svelte";
  import MetricGrid from "./lib/MetricGrid.svelte";
  import PanelTitle from "./lib/PanelTitle.svelte";
  import ResponseFieldGuide from "./lib/ResponseFieldGuide.svelte";
  import Textarea from "./lib/Textarea.svelte";

  const DEFAULT_API_KEY = "local-dev-key";
  const DEFAULT_BASE_URL = "http://127.0.0.1:8000/v1";
  const LOCAL_API_HOST = "127.0.0.1";
  const LOCAL_API_PORT = "8000";
  const ACCOUNT_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
  const ACCOUNT_NAME_HELP =
    "Use English letters, numbers, dash, or underscore only. Example: pro-main, free-2, plus_work.";
  const PLANS = ["free", "go", "plus", "pro"] as const;
  const FEATURES = [
    ["chat", "Chat", "Total conversation calls per account"],
    [
      "upload",
      "Upload",
      "Input-image upload, OCR, describe, edit, and composite jobs per account",
    ],
    ["image", "Image", "Image jobs per account"],
    ["research", "Research", "Deep Research jobs per account"],
  ] as const;
  const DEFAULT_CONCURRENCY = {
    chat: { plans: { free: 1, go: 2, plus: 3, pro: 4 }, accounts: {} },
    upload: { plans: { free: 1, go: 1, plus: 1, pro: 1 }, accounts: {} },
    image: { plans: { free: 1, go: 1, plus: 2, pro: 3 }, accounts: {} },
    research: { plans: { free: 0, go: 0, plus: 2, pro: 2 }, accounts: {} },
  };
  const HOST_OPTIONS = [
    ["127.0.0.1", "Local only", "Only this machine can call the API."],
    [
      "0.0.0.0",
      "LAN / Docker",
      "Listen on every interface. Set Public base URL to a reachable LAN URL.",
    ],
  ] as const;
  const PORT_OPTIONS = [
    ["8000", "Default API"],
    ["8001", "Second API"],
    ["8787", "Tunnel/dev"],
  ] as const;
  const STRATEGY_OPTIONS = [
    [
      "failover",
      "Failover",
      "Try accounts in order, then move to the next account on recoverable errors.",
    ],
    [
      "quota-aware",
      "Quota aware",
      "Prefer stronger paid accounts and recover when limits are detected.",
    ],
    [
      "round-robin",
      "Round robin",
      "Rotate account order for separate requests.",
    ],
    [
      "random",
      "Random",
      "Shuffle account order per request while keeping failover fallbacks.",
    ],
    ["sticky", "Sticky", "Use the first selected account only."],
    [
      "weighted",
      "Weighted",
      "Repeat paid accounts more often in the route order.",
    ],
  ] as const;
  const AGENT_MODE_OPTIONS = [
    ["optimized", "Optimized", "Compact bridge prompt for normal agent work."],
    [
      "opencode",
      "Full opencode",
      "Preserve more of opencode's original prompt detail.",
    ],
  ] as const;
  const FALLBACK_OPTIONS = [
    [
      "auto",
      "Auto",
      "Recover to ChatGPT Web auto when a selected model is limited.",
    ],
    ["none", "None", "Return the real model error to the client."],
  ] as const;
  const TIMEOUT_OPTIONS = [
    ["180", "Chat / image only"],
    ["900", "Medium tasks"],
    ["5400", "Deep Research ready"],
  ] as const;
  const API_FIELD_GUIDES = [
    {
      title: "Chat request body",
      route: "POST /v1/chat/completions",
      fields: [
        {
          name: "model",
          type: "string",
          defaultValue: "auto",
          meaning:
            "Selects the ChatGPT Web model or bridge mode. Use auto for Free/Go. Paid accounts can use discovered /v1/models entries.",
          recommended:
            "auto for normal apps, chatgpt-web/auto@optimized for opencode, chatgpt-deep-research for research.",
          gotcha:
            "This is not a full OpenAI model catalog. It maps to ChatGPT Web account capability and can fail if that account is limited.",
        },
        {
          name: "messages",
          type: "array",
          defaultValue: "required",
          meaning:
            "Conversation transcript. Send the context your app wants the model to know; the bridge does not store your game/chat memory for you.",
          recommended:
            "Include a system message for app rules, then user/assistant turns you want carried forward.",
          gotcha:
            "Deep Research only accepts a latest user message. Do not send follow-up assistant/tool turns with that request.",
        },
        {
          name: "stream",
          type: "boolean",
          defaultValue: "false",
          meaning:
            "When true, returns server-sent events so the UI can render text as it arrives.",
          recommended:
            "Use true for chat UI and opencode. Use false for one-shot scripts.",
          gotcha:
            "Keep the X-ChatGPT-Operation-Id response header so your client can cancel if it disconnects.",
        },
        {
          name: "tools / tool_choice",
          type: "array / string",
          defaultValue: "optional",
          meaning:
            "Enables the agent bridge. The model returns tool call JSON that your client executes.",
          recommended:
            "Use only for opencode-style agents. Normal apps should omit tools.",
          gotcha:
            "Tool execution is client-side. The bridge never runs shell commands by itself.",
        },
        {
          name: "thinking_effort",
          type: "string",
          defaultValue: "model alias decides",
          meaning: "Optional effort override for thinking/pro model aliases.",
          recommended:
            "Prefer model aliases from /v1/models, such as gpt-5-5-thinking-extended.",
          gotcha:
            "Free/Go should stay auto; unsupported effort values route to model errors or fallback.",
        },
        {
          name: "temporary_chat",
          type: "boolean",
          defaultValue: "server setting",
          meaning:
            "Requests ChatGPT temporary/private chat for normal chat calls.",
          recommended: "Default true for chat privacy.",
          gotcha:
            "Image generation and Deep Research force normal chat because ChatGPT Web does not support them in temporary mode.",
        },
        {
          name: "metadata",
          type: "object",
          defaultValue: "optional",
          meaning:
            "Bridge-specific switches can also be passed here, such as agent_mode, deep_research, output_dir, route overrides, or operation id.",
          recommended:
            "Use metadata for app-owned values so your normal message content stays clean.",
          gotcha: "Do not put secrets into metadata if you store transcripts.",
        },
        {
          name: "chatgpt_account",
          type: "string",
          defaultValue: "server router",
          meaning:
            "Request one local account alias for this call. Same as CLI --account.",
          recommended:
            "Use for debug, paid-only workflows, or a user-selected account.",
          gotcha:
            "This is a local alias like pro-main or free-2, not a ChatGPT plan name.",
        },
        {
          name: "chatgpt_accounts",
          type: "array / comma string",
          defaultValue: "server accounts",
          meaning:
            "Restricts this request to a set of local account aliases. Same as CLI --accounts.",
          recommended:
            "Use with chatgpt_account_strategy for per-request failover or quota-aware routing.",
          gotcha:
            "Unknown aliases are rejected before provider work starts.",
        },
        {
          name: "chatgpt_account_strategy",
          type: "auto | sticky | failover | random | round-robin | weighted | quota-aware",
          defaultValue: "server strategy",
          meaning:
            "Per-request account routing strategy. Same as CLI --account-strategy.",
          recommended:
            "failover for simple apps, random for loose free-account pools, quota-aware for image/research-heavy apps after testing.",
          gotcha:
            "Local routing cannot bypass ChatGPT Web hidden rate limits.",
        },
      ],
    },
    {
      title: "Image request body",
      route: "POST /v1/images/generations",
      fields: [
        {
          name: "prompt",
          type: "string",
          defaultValue: "required",
          meaning: "The visual description sent to ChatGPT image generation.",
          recommended:
            "Describe the scene, subject, style, camera, and explicitly say no UI/no text when needed.",
          gotcha:
            "If the prompt describes a game screen, ChatGPT may draw UI instead of the story scene.",
        },
        {
          name: "n",
          type: "integer",
          defaultValue: "1",
          meaning:
            "Number of images requested. The bridge currently supports only one image per call.",
          recommended: "Always send 1 or omit it.",
          gotcha: "Any value other than 1 returns an invalid_request_error.",
        },
        {
          name: "response_format",
          type: "string",
          defaultValue: "url",
          meaning:
            "Controls response shape. url returns a bridge download URL after saving the file locally.",
          recommended: "url for browser apps and LAN clients.",
          gotcha:
            "The bridge saves completed images only. If no image asset comes back, no library item should appear.",
        },
        {
          name: "output_dir",
          type: "string",
          defaultValue: "server image output dir",
          meaning: "Directory where generated images are saved.",
          recommended:
            "Use a mounted volume in Docker, for example /data/chatgpt-api/images.",
          gotcha:
            "LAN users need Public base URL configured or the returned 127.0.0.1 URL will point at their own machine.",
        },
        {
          name: "output_path / path",
          type: "string",
          defaultValue: "auto filename",
          meaning: "Exact save path for this generated image.",
          recommended:
            "Use only when the user explicitly asks for a destination path.",
          gotcha:
            "The process must have write permission to the parent directory.",
        },
        {
          name: "chatgpt_operation_id",
          type: "string",
          defaultValue: "auto",
          meaning: "Client-provided id used later to cancel the image job.",
          recommended:
            "Generate one in frontend state before starting a long image request.",
          gotcha:
            "Cancel is best effort because ChatGPT may already have finished the backend job.",
        },
      ],
    },
    {
      title: "Image edit/composite request body",
      route: "POST /v1/images/edits",
      fields: [
        {
          name: "prompt",
          type: "string",
          defaultValue: "required",
          meaning:
            "Instruction for editing or combining the uploaded source image(s).",
          recommended:
            "Say exactly what to preserve, what to change, and whether multiple inputs should be merged into one new image.",
          gotcha:
            "The route accepts up to 10 input images, but returns exactly one completed output image.",
        },
        {
          name: "image / images / input_images",
          type: "path / URL / data URL / base64 / array",
          defaultValue: "required",
          meaning:
            "Source image inputs uploaded to ChatGPT before the edit prompt is sent.",
          recommended:
            "Use local paths on the API host, public URLs, or data URLs from browser clients. These inputs need file_upload capacity when ChatGPT reports it.",
          gotcha:
            "All OCR/edit/composite calls share the upload concurrency bucket and ChatGPT file_upload quota. not_reported is treated as unknown capacity, not blocked.",
        },
        {
          name: "aspect_ratio",
          type: "auto | 1:1 | 3:4 | 9:16 | 4:3 | 16:9",
          defaultValue: "auto",
          meaning:
            "Requested output composition ratio. The bridge appends this to the ChatGPT prompt.",
          recommended:
            "Use the same ratio as the source image when layout or position must stay stable.",
          gotcha:
            "Large warning: if auto receives a source image that does not match one of the supported ratios, output size and object positions can shift.",
        },
        {
          name: "output_dir / output_path",
          type: "string",
          defaultValue: "server image output dir",
          meaning: "Where the final edited image is saved after ChatGPT returns a real asset.",
          recommended:
            "Use output_path only when the user explicitly chose a destination.",
          gotcha:
            "The storage library only registers completed files, not started or failed jobs.",
        },
      ],
    },
    {
      title: "Vision/OCR request body",
      route: "POST /v1/chatgpt/vision",
      fields: [
        {
          name: "mode",
          type: "custom | ocr | describe",
          defaultValue: "custom",
          meaning:
            "Selects a default prompt when prompt is omitted. custom lets your app provide its own OCR/analysis prompt.",
          recommended:
            "Use ocr for text extraction, describe for image understanding, and custom for app-specific analysis.",
          gotcha:
            "The route returns assistant text fields. That text can be plain OCR, markdown, or strict JSON/bbox if your prompt requests it. Use /v1/images/edits when you expect an image artifact.",
        },
        {
          name: "prompt",
          type: "string",
          defaultValue: "mode default",
          meaning:
            "Question or extraction instruction sent with the uploaded images.",
          recommended:
            "Tell ChatGPT what format to return, such as plain text, markdown, strict JSON, or an items[] schema with estimated bbox coordinates.",
          gotcha:
            "The bridge does not validate OCR JSON for you. Bbox coordinates are model-estimated, not native OCR-engine layout coordinates.",
        },
        {
          name: "image / images / input_images",
          type: "path / URL / data URL / base64 / array",
          defaultValue: "required",
          meaning: "One to 10 images uploaded into the ChatGPT conversation.",
          recommended:
            "Keep each request focused. These inputs consume the account file_upload bucket when ChatGPT reports it.",
          gotcha:
            "not_reported means the account did not expose that counter, not that the route is blocked. Hidden burst limits can still happen.",
        },
      ],
    },
    {
      title: "Deep Research request body",
      route: "POST /v1/chat/completions",
      fields: [
        {
          name: "model",
          type: "string",
          defaultValue: "chatgpt-deep-research",
          meaning:
            "Triggers the Deep Research connector instead of normal chat.",
          recommended:
            "Use chatgpt-deep-research or metadata.deep_research=true. chatgpt-web/deep-research is kept as a legacy alias.",
          gotcha:
            "Free accounts can be blocked. Use normal chat mode only; temporary chat is not supported.",
        },
        {
          name: "deep_research",
          type: "boolean",
          defaultValue: "false",
          meaning:
            "Alternative trigger when you do not want to use the deep-research model alias.",
          recommended:
            "Use this for app buttons labeled Research so model selection remains separate.",
          gotcha:
            "Long timeout is required. Set Web timeout to Deep Research ready or pass a high client timeout.",
        },
        {
          name: "system_hints",
          type: "array",
          defaultValue: "optional",
          meaning:
            "Low-level ChatGPT connector hint. connector:connector_openai_deep_research also triggers research.",
          recommended:
            "Use the model alias unless you are reproducing a raw ChatGPT Web request.",
          gotcha: "This is ChatGPT-Web-specific and not portable.",
        },
        {
          name: "output_dir",
          type: "string",
          defaultValue: "server research output dir",
          meaning:
            "Directory where the final markdown research report is saved.",
          recommended:
            "Use a mounted volume for Docker and a public base URL for downloads.",
          gotcha:
            "The chat response intentionally returns only done/path/download URL, not the full report body.",
        },
        {
          name: "chatgpt_operation_id",
          type: "string",
          defaultValue: "auto",
          meaning:
            "Client-provided id used by /v1/chatgpt/operations/{id}/cancel.",
          recommended:
            "Generate one before starting research so another terminal, tab, or AbortController can cancel it.",
          gotcha:
            "Operation ids are live runtime records, not durable history. Research cancel needs the connector session ids first; the bridge reads them from the WSS widget stream, then sends the Deep Research MCP stop call.",
        },
      ],
    },
    {
      title: "Admin/account body fields",
      route: "/v1/chatgpt/admin/*",
      fields: [
        {
          name: "account",
          type: "string",
          defaultValue: "required on account routes",
          meaning:
            "Local account slug such as free, pro, plus-work, or plus_01.",
          recommended:
            "Use English letters, numbers, dash, or underscore only. Save a fresh capture over the same name when tokens expire.",
          gotcha:
            "Thai names, spaces, dots, and leading dashes are rejected because the name is used in local paths and routing keys.",
        },
        {
          name: "capture_text",
          type: "string",
          defaultValue: "required for save",
          meaning:
            "Copied Network request details from POST /backend-api/f/conversation.",
          recommended:
            "Paste Chrome/Safari Headers + Payload, Safari Request Data, or full Copy as cURL output with Authorization, Cookie or -b cookies, and --data-raw.",
          gotcha:
            "Do not commit capture files; they contain session cookies/tokens.",
        },
        {
          name: "settings.concurrency",
          type: "object",
          defaultValue: "recommended plan defaults",
          meaning:
            "Local per-plan and per-account semaphores for chat, upload, image, and research.",
          recommended:
            "Chat pro 4, plus 3, go 2, free 1. Upload 1 for every plan. Image pro 3. Research plus/pro 2.",
          gotcha:
            "These are local throttles only. ChatGPT can still apply hidden burst cooldowns.",
        },
      ],
    },
  ] as const;
  const RESPONSE_FIELD_GUIDES = [
    {
      name: "choices[0].message.content",
      where: "chat",
      meaning: "Assistant text. Your app should read this for normal chat.",
    },
    {
      name: "choices[0].message.tool_calls",
      where: "agent bridge",
      meaning:
        "Tool call list for opencode-style clients. Execute client-side, then send tool results back.",
    },
    {
      name: "chatgpt_account",
      where: "chat/vision/image/edit/research",
      meaning: "Which local account actually handled the request.",
    },
    {
      name: "chatgpt_fallback_model",
      where: "chat",
      meaning:
        "Present when the bridge recovered from a limited model to fallback.",
    },
    {
      name: "data[0].download_url",
      where: "image / image edit",
      meaning: "HTTP URL to the saved image through the bridge download route.",
    },
    {
      name: "data[0].path",
      where: "image / image edit",
      meaning: "Local filesystem path where the completed image was saved.",
    },
    {
      name: "text",
      where: "vision / OCR",
      meaning:
        "Convenience field containing the same assistant output as choices[0].message.content. It can be plain OCR text, markdown, or a JSON string if your prompt requested structured OCR/bbox.",
    },
    {
      name: "input_image_count",
      where: "vision / image edit",
      meaning: "How many source images were uploaded into the ChatGPT request.",
    },
    {
      name: "warnings[]",
      where: "image edit",
      meaning:
        "Aspect-ratio and source-image warnings your UI should surface before users expect pixel-stable edits.",
    },
    {
      name: "chatgpt_research_report_download_url",
      where: "research",
      meaning: "HTTP URL to the saved markdown research report.",
    },
    {
      name: "chatgpt_operation_id",
      where: "image / streaming header",
      meaning:
        "Use with /chatgpt/operations/{id}/cancel when users close tabs or press Ctrl-C.",
    },
  ] as const;
  const LAUNCH_FIELD_GUIDES = [
    [
      "Host",
      "Where the API listens. 127.0.0.1 is local-only; 0.0.0.0 is for LAN/Docker.",
    ],
    [
      "Port",
      "The local TCP port for the bridge API. Use a second port when another server is already running.",
    ],
    [
      "API key",
      "Bearer token clients must send. Default dev key is local-dev-key; change it before LAN use.",
    ],
    [
      "Public base URL",
      "The URL clients and download links should use. For LAN, use the machine IP, not 127.0.0.1.",
    ],
    [
      "Primary account",
      "First account in the route. Usually pro for testing, free for free-only proof.",
    ],
    [
      "Accounts",
      "All accounts the router may use. Multiple accounts enable failover and concurrency.",
    ],
    [
      "Strategy",
      "How account order is chosen: failover, random, quota-aware, round-robin, sticky, or weighted.",
    ],
    [
      "Agent prompt mode",
      "optimized is compact. opencode preserves more original opencode prompt text.",
    ],
    [
      "Model fallback",
      "auto recovers from limited selected models; none surfaces the real error.",
    ],
    [
      "Temporary chat",
      "Default privacy mode for chat. Image and Deep Research override to normal chat.",
    ],
    [
      "Web timeout",
      "Maximum wait for web jobs. Deep Research needs the longest preset.",
    ],
    [
      "Output paths",
      "Local directories/DB file for images, research reports, and console metadata.",
    ],
    [
      "Concurrency",
      "Local limits per plan/account. They prevent your own app from flooding one ChatGPT account.",
    ],
  ] as const;

  type Json = any;
  type AccountRow = {
    account: string;
    configured?: boolean;
    capture_exists?: boolean;
    settings_exists?: boolean;
    capture_path?: string;
    settings_path?: string;
    plan_type?: string;
    plan_bucket?: string;
    profile?: Json;
    capabilities?: Json;
    stored?: Json;
    profile_error?: string;
  };
  type LiveAccount = {
    account: string;
    ok?: boolean;
    error?: string;
    default_model_slug?: string;
    plan_type?: string;
    plan_bucket?: string;
    features?: Record<string, Json>;
    model_limits?: Json[];
    profile_error?: string;
  };
  type Artifact = {
    file_id: string;
    kind: string;
    filename: string;
    path: string;
    download_url: string;
    content_type?: string;
    bytes?: number;
    account?: string;
    prompt?: string;
    created_at?: string;
    exists?: boolean;
  };
  type ModelRow = {
    id: string;
    object?: string;
    created?: number;
    owned_by?: string;
    name?: string;
  };
  type CapacityAccount = {
    name: string;
    plan: string;
    limit: number;
    status: string;
    detail: string;
    tone: "ok" | "warn" | "bad" | "muted";
  };
  type CapacityCard = {
    id: string;
    label: string;
    title: string;
    route: string;
    model: string;
    parallel: number;
    quota: string;
    output: string;
    note: string;
    tone: "ok" | "warn" | "bad" | "muted";
    accounts: CapacityAccount[];
  };
  type ModelGroup = {
    id: string;
    title: string;
    note: string;
    ids: string[];
  };

  const pages = [
    ["overview", "Overview", "Run status, setup flow, live accounts"],
    ["accounts", "Accounts", "Paste captures, repair broken accounts"],
    ["test-lab", "Test Lab", "Chat, context, image, and research calls"],
    ["limits", "Limits", "Per-plan and per-account runtime throttles"],
    ["api-docs", "Docs", "API, CLI, Docker, and route examples"],
    ["storage", "Library", "Preview generated images and reports"],
    ["opencode", "opencode", "Local inject and LAN client setup"],
    ["settings", "Launch", "Server presets and command builder"],
  ] as const;

  let page = $state("overview");
  let busy = $state("");
  let toast = $state("");
  let toastTone = $state<"ok" | "bad">("ok");
  let status = $state<Json | null>(null);
  let settings = $state<Json | null>(null);
  let usage = $state<Json | null>(null);
  let accounts = $state<AccountRow[]>([]);
  let liveByAccount = $state<Record<string, LiveAccount>>({});
  let artifacts = $state<Artifact[]>([]);
  let modelRows = $state<ModelRow[]>([]);
  let lastError = $state("");
  let apiLatencyMs = $state<number | null>(null);
  let lastHealthCheckAt = $state("");
  let usageCheckedAt = $state("");
  let modelsCheckedAt = $state("");
  let commandPaletteOpen = $state(false);
  let commandQuery = $state("");

  let baseUrl = $state(DEFAULT_BASE_URL);
  let apiKey = $state(DEFAULT_API_KEY);

  let captureAccount = $state("free");
  let captureText = $state("");
  let captureResult = $state<Json | null>(null);
  let captureModalOpen = $state(false);
  let newCaptureAccount = $state("plus-work");
  let newCaptureText = $state("");
  let newCaptureResult = $state<Json | null>(null);

  let chatModel = $state("auto");
  let chatPrompt = $state(
    "Reply in one sentence: the local bridge is working.",
  );
  let chatResult = $state("");
  let contextSystem = $state(
    "You are a memory test assistant. Answer only from the provided conversation.",
  );
  let contextSetup = $state(
    "My name is Mira. I carry a silver key and can cast Healing.",
  );
  let contextAssistant = $state("Noted: Mira has a silver key and Healing.");
  let contextQuestion = $state("What name, item, and skill did I give you?");
  let contextResult = $state("");
  let researchPrompt = $state(
    "Briefly research whether LLMs could reach AGI. Keep it concise.",
  );
  let researchResult = $state("");

  let imageModel = $state("auto");
  let imagePrompt = $state(
    "A cinematic dark fantasy portal opening in a rainy neon alley, no UI, no text",
  );
  let imageResult = $state<Json | null>(null);

  let opencodePath = $state("");
  let opencodeModel = $state("chatgpt-web/auto@optimized");

  let serverHost = $state("127.0.0.1");
  let serverPort = $state("8000");
  let serverKey = $state(DEFAULT_API_KEY);
  let serverAccount = $state("");
  let serverAccounts = $state("");
  let serverStrategy = $state("failover");
  let serverPublicBase = $state("http://127.0.0.1:8000/v1");
  let imageOutputDir = $state("outputs/chatgpt-images");
  let researchOutputDir = $state("outputs/chatgpt-research");
  let adminDbPath = $state("outputs/chatgpt-admin.sqlite");
  let webTimeout = $state("5400");
  let agentMode = $state("optimized");
  let modelFallback = $state("auto");
  let temporaryChat = $state(true);
  let selectedPreset = $state("local");
  let concurrency = $state<Json>(cloneJson(DEFAULT_CONCURRENCY));
  let settingsResult = $state("");

  const currentPage = $derived(pages.find(([id]) => id === page) ?? pages[0]);
  const server = $derived((status?.server ?? {}) as Json);
  const routing = $derived((status?.routing ?? {}) as Json);
  const storage = $derived((status?.storage ?? {}) as Json);
  const mergedAccounts = $derived(
    accounts.map((account) => ({
      ...account,
      live: liveByAccount[account.account],
    })),
  );
  const accountNames = $derived(
    Array.from(
      new Set(["free", "go", "plus", "pro", ...accounts.map((account) => account.account)]),
    ).filter(Boolean),
  );
  const selectedServerAccounts = $derived(splitCsv(serverAccounts));
  const chatCurl = $derived(
    curl("POST", "/chat/completions", {
      model: chatModel || "auto",
      messages: [{ role: "user", content: chatPrompt }],
      stream: false,
    }),
  );
  const contextMessages = $derived([
    { role: "system", content: contextSystem },
    { role: "user", content: contextSetup },
    { role: "assistant", content: contextAssistant },
    { role: "user", content: contextQuestion },
  ]);
  const contextCurl = $derived(
    curl("POST", "/chat/completions", {
      model: chatModel || "auto",
      messages: contextMessages,
      stream: false,
    }),
  );
  const imageCurl = $derived(
    curl("POST", "/images/generations", {
      model: imageModel || "auto",
      prompt: imagePrompt,
      n: 1,
      response_format: "url",
    }),
  );
  const serveCommand = $derived(buildServeCommand());
  const opencodeQuickCommand = $derived(
    `bun integrations/opencode/opencode-config.mjs --base-url ${quoteShell(baseUrl)} --api-key ${quoteShell(apiKey || DEFAULT_API_KEY)}`,
  );
  const consoleCommand = $derived("bun --cwd apps/bridge-console dev");
  const consoleLanCommand = $derived("bun --cwd apps/bridge-console dev:lan");
  const opencodeLanJson = $derived(buildOpencodeProviderJson());
  const opencodeLanCurl = $derived(
    curl("POST", "/chatgpt/admin/opencode/inject", {
      base_url: baseUrl,
      api_key: apiKey || DEFAULT_API_KEY,
      model: opencodeModel || "chatgpt-web/auto@optimized",
    }),
  );
  const apiDocs = $derived(buildApiDocs());
  const cliDocs = $derived(buildCliDocs());
  const responseExamples = $derived(buildResponseExamples());
  const routeResponseDocs = $derived(buildRouteResponseDocs());
  const downloadGuides = $derived(buildDownloadGuides());
  const usageAccounts = $derived(
    Array.isArray(usage?.accounts) ? usage.accounts : [],
  );
  const modelIds = $derived(
    modelRows.map((model) => model.id).filter(Boolean),
  );
  const visibleModelIds = $derived(
    modelIds.length ? modelIds : fallbackModelIds(),
  );
  const modelGroups = $derived(buildModelGroups());
  const capacityCards = $derived(buildCapacityCards());
  const activeAccountCount = $derived(
    (routing.accounts ?? accountNames).length,
  );
  const healthyAccountCount = $derived(
    Object.values(liveByAccount).filter((account) => account?.ok).length,
  );
  const checkedAccountCount = $derived(Object.keys(liveByAccount).length);
  const failedAccountCount = $derived(
    Object.values(liveByAccount).filter((account) => account?.ok === false)
      .length,
  );
  const healthyAccountText = $derived(
    checkedAccountCount ? `${healthyAccountCount}/${checkedAccountCount}` : "-",
  );
  const systemState = $derived(
    status ? "online" : lastError ? "offline" : "checking",
  );
  const systemLabel = $derived(
    systemState === "online"
      ? "ONLINE"
      : systemState === "offline"
        ? "OFFLINE"
        : "CHECKING",
  );
  const routeMonitorRows = $derived(buildRouteMonitorRows());
  const eventRows = $derived(buildEventRows());
  const commandActions = $derived(buildCommandActions());
  const filteredCommandActions = $derived(
    commandActions.filter((action) => {
      const query = commandQuery.trim().toLowerCase();
      if (!query) return true;
      return (
        action.title.toLowerCase().includes(query) ||
        action.detail.toLowerCase().includes(query)
      );
    }),
  );

  onMount(() => {
    baseUrl =
      localStorage.getItem("chatgpt.console.baseUrl") || DEFAULT_BASE_URL;
    apiKey = localStorage.getItem("chatgpt.console.apiKey") ?? DEFAULT_API_KEY;
    const hash = location.hash.replace("#", "");
    if (pages.some(([id]) => id === hash)) page = hash;
    const onHashChange = () => {
      const next = location.hash.replace("#", "");
      if (pages.some(([id]) => id === next)) page = next;
    };
    const onToast = (event: Event) => {
      const message = (event as CustomEvent<string>).detail;
      if (message) showToast(message);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        commandPaletteOpen = true;
        commandQuery = "";
      }
      if (event.key === "Escape") {
        commandPaletteOpen = false;
      }
    };
    window.addEventListener("hashchange", onHashChange);
    window.addEventListener("chatgpt-console-toast", onToast);
    window.addEventListener("keydown", onKeyDown);
    void refreshAll();
    return () => {
      window.removeEventListener("hashchange", onHashChange);
      window.removeEventListener("chatgpt-console-toast", onToast);
      window.removeEventListener("keydown", onKeyDown);
    };
  });

  function setPage(next: string) {
    page = next;
    location.hash = next;
  }

  function apiUrl(path: string) {
    return `${baseUrl.replace(/\/+$/, "")}${path}`;
  }

  async function apiFetch(path: string, options: RequestInit = {}) {
    const response = await fetch(apiUrl(path), {
      ...options,
      headers: {
        ...(options.body ? { "Content-Type": "application/json" } : {}),
        ...(apiKey ? { Authorization: `Bearer ${apiKey}` } : {}),
        ...(options.headers ?? {}),
      },
    });
    const contentType = response.headers.get("content-type") ?? "";
    const payload = contentType.includes("application/json")
      ? await response.json()
      : await response.text();
    if (!response.ok) {
      const message =
        payload?.error?.message || payload || `HTTP ${response.status}`;
      throw new Error(String(message));
    }
    return payload;
  }

  async function runTask(label: string, fn: () => Promise<void>) {
    busy = label;
    lastError = "";
    try {
      await fn();
    } catch (error) {
      lastError = error instanceof Error ? error.message : String(error);
      showToast(lastError, "bad");
    } finally {
      busy = "";
    }
  }

  async function refreshAll() {
    await runTask("refresh", async () => {
      await Promise.allSettled([
        loadStatus(),
        loadAccounts(),
        loadArtifacts(),
        loadModels(),
      ]);
    });
    void loadUsage().catch((error) => {
      lastError = error instanceof Error ? error.message : String(error);
      showToast("Usage refresh failed", "bad");
    });
  }

  async function loadStatus() {
    const started = performance.now();
    status = await apiFetch("/chatgpt/admin/status");
    apiLatencyMs = Math.max(1, Math.round(performance.now() - started));
    lastHealthCheckAt = new Date().toLocaleTimeString();
    settings = status?.settings ?? null;
    concurrency = normalizeConcurrency(settings?.concurrency);
    serverHost = String(status?.server?.host ?? serverHost);
    serverPort = String(status?.server?.port ?? serverPort);
    serverPublicBase = String(
      status?.server?.public_base_url ?? serverPublicBase,
    );
    serverStrategy = String(
      status?.routing?.account_strategy ?? serverStrategy,
    );
    serverAccounts = Array.isArray(status?.routing?.accounts)
      ? status.routing.accounts.join(",")
      : serverAccounts;
    imageOutputDir = String(
      status?.storage?.image_output_dir ?? imageOutputDir,
    );
    researchOutputDir = String(
      status?.storage?.research_output_dir ?? researchOutputDir,
    );
    adminDbPath = String(status?.storage?.admin_db_path ?? adminDbPath);
  }

  async function saveBridgeSettings() {
    await runTask("save-settings", async () => {
      const payload = await apiFetch("/chatgpt/admin/settings/save", {
        method: "POST",
        body: JSON.stringify({
          settings: {
            concurrency: normalizeConcurrency(concurrency),
          },
        }),
      });
      settings = payload.settings;
      concurrency = normalizeConcurrency(payload.settings?.concurrency);
      settingsResult = JSON.stringify(payload, null, 2);
      await loadStatus();
      showToast("Saved runtime limits");
    });
  }

  async function resetBridgeSettings() {
    if (!confirm("Reset runtime limits to recommended defaults?")) return;
    await runTask("reset-settings", async () => {
      const payload = await apiFetch("/chatgpt/admin/settings/reset", {
        method: "POST",
        body: JSON.stringify({}),
      });
      settings = payload.settings;
      concurrency = normalizeConcurrency(payload.settings?.concurrency);
      settingsResult = JSON.stringify(payload, null, 2);
      await loadStatus();
      showToast("Reset runtime limits");
    });
  }

  async function loadAccounts() {
    const payload = await apiFetch("/chatgpt/admin/accounts");
    accounts = payload.accounts ?? [];
  }

  async function loadModels() {
    const payload = await apiFetch("/models");
    modelRows = (payload.data ?? [])
      .map((model: Json) => ({
        id: String(model.id ?? ""),
        object: model.object,
        created: model.created,
        owned_by: model.owned_by,
        name: model.name,
      }))
      .filter((model: ModelRow) => model.id);
    modelsCheckedAt = new Date().toLocaleTimeString();
  }

  async function refreshModels() {
    await runTask("models", async () => {
      await loadModels();
      showToast("Model catalog refreshed");
    });
  }

  async function loadUsage() {
    const payload = await apiFetch("/chatgpt/usage");
    usage = payload;
    usageCheckedAt = new Date().toLocaleTimeString();
    const next = { ...liveByAccount };
    for (const item of payload.accounts ?? []) {
      next[item.account] = item;
    }
    liveByAccount = next;
  }

  async function refreshUsage() {
    await runTask("usage", async () => {
      await loadUsage();
      showToast("Usage refreshed");
    });
  }

  async function checkAccountsPayload(account?: string) {
    const payload = await apiFetch("/chatgpt/admin/accounts/check", {
      method: "POST",
      body: JSON.stringify(account ? { account } : { account: "all" }),
    });
    const next = { ...liveByAccount };
    for (const item of payload.accounts ?? []) {
      next[item.account] = item;
    }
    liveByAccount = next;
    return payload;
  }

  async function checkAccounts(account?: string) {
    await runTask(account ? `check-${account}` : "check-all", async () => {
      await checkAccountsPayload(account);
      showToast(account ? `Checked ${account}` : "Checked all accounts");
    });
  }

  async function deleteAccount(account: string) {
    if (!confirm(`Delete local capture/settings for account "${account}"?`))
      return;
    await runTask(`delete-${account}`, async () => {
      await apiFetch("/chatgpt/admin/accounts/delete", {
        method: "POST",
        body: JSON.stringify({
          account,
          delete_capture: true,
          delete_settings: true,
        }),
      });
      delete liveByAccount[account];
      liveByAccount = { ...liveByAccount };
      await loadAccounts();
      showToast(`Deleted ${account}`);
    });
  }

  function editAccountCapture(account: string) {
    captureAccount = account;
    captureText = "";
    captureResult = null;
    captureModalOpen = true;
  }

  async function inspectCapturePayload(
    save: boolean,
    account: string,
    capture: string,
  ) {
    const accountName = account.trim();
    if (!ACCOUNT_NAME_RE.test(accountName)) {
      const result = {
        ok: false,
        missing: ["valid account name"],
        warnings: [ACCOUNT_NAME_HELP],
        checks: [
          {
            name: "account_name",
            ok: false,
            level: "required",
            detail: ACCOUNT_NAME_HELP,
          },
        ],
      };
      showToast("Invalid account name. Use English slug only.", "bad");
      return { ok: false, result };
    }
    const task = save
      ? `save-capture-${accountName}`
      : `inspect-capture-${accountName}`;
    let result: Json | null = null;
    let ok = false;
    await runTask(task, async () => {
      const path = save
        ? "/chatgpt/admin/captures/save"
        : "/chatgpt/admin/captures/inspect";
      const response = await fetch(apiUrl(path), {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(apiKey ? { Authorization: `Bearer ${apiKey}` } : {}),
        },
        body: JSON.stringify({
          account: accountName,
          capture_text: capture,
        }),
      });
      const payload = await response.json();
      result = save ? payload.inspection : payload;
      if (!response.ok) {
        const message =
          payload?.error?.message ||
          "Capture did not pass validation. Review the checklist below.";
        throw new Error(message);
      }
      if (save) {
        await loadAccounts();
        const verify = await checkAccountsPayload(accountName);
        const verifiedAccount = (verify.accounts ?? []).find(
          (item: Json) => item?.account === accountName,
        );
        if (!verifiedAccount?.ok) {
          throw new Error(
            `Saved capture, but live verification failed: ${
              verifiedAccount?.error ||
              verifiedAccount?.warning ||
              verifiedAccount?.status ||
              "unknown account check error"
            }`,
          );
        }
        await loadUsage();
        showToast(`Saved ${accountName}`);
      }
      ok = true;
    });
    return { ok, result };
  }

  async function inspectExistingCapture(save = false) {
    const next = await inspectCapturePayload(save, captureAccount, captureText);
    captureResult = next.result;
    if (save && next.ok) {
      const savedAccount = captureAccount.trim();
      closeCaptureModal();
      showToast(`Updated ${savedAccount}`);
    }
  }

  async function inspectNewCapture(save = false) {
    const next = await inspectCapturePayload(
      save,
      newCaptureAccount,
      newCaptureText,
    );
    newCaptureResult = next.result;
    if (save && next.ok) {
      showToast(`Added ${newCaptureAccount.trim()}`);
      captureAccount = newCaptureAccount.trim();
      newCaptureAccount = "";
      newCaptureText = "";
      newCaptureResult = null;
    }
  }

  function closeCaptureModal() {
    captureModalOpen = false;
    captureText = "";
    captureResult = null;
  }

  async function runChat() {
    await runTask("chat-test", async () => {
      chatResult = "Running...";
      const payload = await apiFetch("/chatgpt/admin/test/chat", {
        method: "POST",
        body: JSON.stringify({
          model: chatModel || "auto",
          message: chatPrompt,
        }),
      });
      chatResult = `${payload.latency_ms}ms\n\n${payload.content || JSON.stringify(payload.response, null, 2)}`;
    });
  }

  async function runContextChat() {
    await runTask("context-chat", async () => {
      contextResult = "Running...";
      const payload = await apiFetch("/chat/completions", {
        method: "POST",
        body: JSON.stringify({
          model: chatModel || "auto",
          messages: contextMessages,
          stream: false,
        }),
      });
      contextResult =
        payload.choices?.[0]?.message?.content ||
        JSON.stringify(payload, null, 2);
    });
  }

  async function runResearch() {
    if (
      !confirm(
        "Deep Research can take a long time and consume research quota. Start it now?",
      )
    ) {
      return;
    }
    await runTask("deep-research", async () => {
      researchResult = "Research running...";
      const payload = await apiFetch("/chat/completions", {
        method: "POST",
        body: JSON.stringify({
          model: "chatgpt-deep-research",
          messages: [{ role: "user", content: researchPrompt }],
          output_dir: researchOutputDir,
        }),
      });
      researchResult =
        payload.choices?.[0]?.message?.content ||
        JSON.stringify(payload, null, 2);
      await loadArtifacts();
    });
  }

  async function runImage() {
    await runTask("image-test", async () => {
      imageResult = null;
      const payload = await apiFetch("/chatgpt/admin/test/image", {
        method: "POST",
        body: JSON.stringify({
          model: imageModel || "auto",
          prompt: imagePrompt,
        }),
      });
      imageResult = payload.response;
      await loadArtifacts();
    });
  }

  async function loadArtifacts() {
    const payload = await apiFetch("/chatgpt/admin/artifacts?limit=200");
    artifacts = payload.artifacts ?? [];
  }

  async function deleteArtifact(artifact: Artifact, deleteFile: boolean) {
    const target = deleteFile ? "metadata and file" : "metadata only";
    if (!confirm(`Delete ${target} for "${artifact.filename}"?`)) return;
    await runTask(`delete-artifact-${artifact.file_id}`, async () => {
      await apiFetch("/chatgpt/admin/artifacts/delete", {
        method: "POST",
        body: JSON.stringify({
          file_id: artifact.file_id,
          delete_file: deleteFile,
        }),
      });
      await loadArtifacts();
      showToast(
        deleteFile ? "Deleted artifact file" : "Deleted artifact record",
      );
    });
  }

  async function injectOpencode() {
    await runTask("inject-opencode", async () => {
      await apiFetch("/chatgpt/admin/opencode/inject", {
        method: "POST",
        body: JSON.stringify({
          config_path: opencodePath || undefined,
          base_url: baseUrl,
          api_key: apiKey,
          model: opencodeModel || "chatgpt-web/auto@optimized",
        }),
      });
      showToast("Injected opencode config");
    });
  }

  async function ejectOpencode() {
    await runTask("eject-opencode", async () => {
      await apiFetch("/chatgpt/admin/opencode/eject", {
        method: "POST",
        body: JSON.stringify({ config_path: opencodePath || undefined }),
      });
      showToast("Ejected opencode config");
    });
  }

  function saveConnection() {
    localStorage.setItem(
      "chatgpt.console.baseUrl",
      baseUrl || DEFAULT_BASE_URL,
    );
    localStorage.setItem("chatgpt.console.apiKey", apiKey);
    showToast("Saved console connection");
  }

  function cloneJson<T>(value: T): T {
    return JSON.parse(JSON.stringify(value));
  }

  function normalizeConcurrency(value: Json) {
    const next = cloneJson(DEFAULT_CONCURRENCY) as Json;
    const source = value && typeof value === "object" ? value : {};
    for (const [feature] of FEATURES) {
      const featureValue = source[feature] ?? {};
      const plans = featureValue.plans ?? {};
      const accountsValue = featureValue.accounts ?? {};
      for (const plan of PLANS) {
        next[feature].plans[plan] = limitNumber(
          plans[plan],
          next[feature].plans[plan],
          feature !== "chat",
        );
      }
      const accountsMap: Record<string, number> = {};
      if (accountsValue && typeof accountsValue === "object") {
        for (const [account, limit] of Object.entries(accountsValue)) {
          const name = String(account).trim();
          if (!name) continue;
          accountsMap[name] = limitNumber(limit, 1, feature !== "chat");
        }
      }
      next[feature].accounts = accountsMap;
    }
    return next;
  }

  function limitNumber(value: unknown, fallback: number, allowZero: boolean) {
    const parsed = Number.parseInt(String(value ?? ""), 10);
    const lower = allowZero ? 0 : 1;
    if (!Number.isFinite(parsed)) return fallback;
    return Math.max(lower, Math.min(parsed, 32));
  }

  function setPlanLimit(feature: string, plan: string, value: string) {
    const current = normalizeConcurrency(concurrency);
    current[feature].plans[plan] = limitNumber(
      value,
      current[feature].plans[plan],
      feature !== "chat",
    );
    concurrency = current;
  }

  function setAccountLimit(feature: string, account: string, value: string) {
    const current = normalizeConcurrency(concurrency);
    const name = account.trim();
    if (!name) return;
    current[feature].accounts[name] = limitNumber(
      value,
      current[feature].accounts[name] ??
        current[feature].plans[planForAccount(account)] ??
        1,
      feature !== "chat",
    );
    concurrency = current;
  }

  function clearAccountLimit(feature: string, account: string) {
    const current = normalizeConcurrency(concurrency);
    delete current[feature].accounts[account];
    concurrency = current;
  }

  function planForAccount(account: string) {
    const lowered = account.toLowerCase();
    if (lowered.includes("pro")) return "pro";
    if (lowered.includes("plus")) return "plus";
    if (lowered.includes("go")) return "go";
    return "free";
  }

  function concurrencyArg(feature: string) {
    const current = normalizeConcurrency(concurrency);
    const items = [
      ...PLANS.map((plan) => `${plan}=${current[feature].plans[plan]}`),
      ...Object.entries(current[feature].accounts ?? {}).map(
        ([account, limit]) => `${account}=${limit}`,
      ),
    ];
    return items.join(",");
  }

  function resetDefaultConnection() {
    baseUrl = DEFAULT_BASE_URL;
    apiKey = DEFAULT_API_KEY;
    saveConnection();
  }

  function applyServerPreset(preset: "local" | "lan" | "pro" | "free") {
    selectedPreset = preset;
    if (preset === "local") {
      serverHost = LOCAL_API_HOST;
      serverPort = LOCAL_API_PORT;
      serverPublicBase = `http://${LOCAL_API_HOST}:${LOCAL_API_PORT}/v1`;
      serverStrategy = "failover";
      serverAccounts = "";
      serverAccount = "";
    } else if (preset === "lan") {
      serverHost = "0.0.0.0";
      serverPort = LOCAL_API_PORT;
      serverPublicBase = `http://YOUR-LAN-IP:${LOCAL_API_PORT}/v1`;
      serverStrategy = "failover";
      serverAccounts = "";
      serverAccount = "";
    } else if (preset === "pro") {
      serverHost = LOCAL_API_HOST;
      serverPort = LOCAL_API_PORT;
      serverPublicBase = `http://${LOCAL_API_HOST}:${LOCAL_API_PORT}/v1`;
      serverStrategy = "quota-aware";
      serverAccounts = "pro";
      serverAccount = "pro";
    } else {
      serverHost = LOCAL_API_HOST;
      serverPort = LOCAL_API_PORT;
      serverPublicBase = `http://${LOCAL_API_HOST}:${LOCAL_API_PORT}/v1`;
      serverStrategy = "sticky";
      serverAccounts = "free";
      serverAccount = "free";
      modelFallback = "auto";
    }
    showToast(`Applied ${preset} preset`);
  }

  function presetCards() {
    return [
      {
        id: "local",
        title: "Local dev",
        body: "Use this machine only. Good default for testing the bridge and console.",
        values: "host 127.0.0.1 · accounts auto-discover · failover",
      },
      {
        id: "lan",
        title: "LAN share",
        body: "Let phones or another computer call this API through your LAN IP.",
        values: "host 0.0.0.0 · public URL needs LAN IP",
      },
      {
        id: "pro",
        title: "Pro only",
        body: "Use one paid account for heavier tests without touching free quota.",
        values: "accounts pro · quota-aware · model fallback auto",
      },
      {
        id: "free",
        title: "Free safe mode",
        body: "Keep the route conservative. Best for checking basic chat only.",
        values: "accounts free · sticky · auto model",
      },
    ] as const;
  }

  function showToast(message: string, tone: "ok" | "bad" = "ok") {
    toast = message;
    toastTone = tone;
    window.setTimeout(() => {
      if (toast === message) toast = "";
    }, 3200);
  }

  function splitCsv(value: string) {
    return value
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);
  }

  function setServerAccounts(next: string[]) {
    const unique = Array.from(new Set(next.filter(Boolean)));
    serverAccounts = unique.join(",");
    if (!unique.includes(serverAccount)) {
      serverAccount = unique[0] ?? "pro";
    }
  }

  function toggleServerAccount(account: string) {
    const selected = selectedServerAccounts.includes(account)
      ? selectedServerAccounts.filter((item) => item !== account)
      : [...selectedServerAccounts, account];
    setServerAccounts(selected.length ? selected : [account]);
  }

  function quickOutputRoot(root: string) {
    imageOutputDir = `${root}/images`;
    researchOutputDir = `${root}/research`;
    adminDbPath = `${root}/chatgpt-admin.sqlite`;
  }

  function curl(method: string, path: string, body?: unknown) {
    const target = apiUrl(path);
    const headers = [`Authorization: Bearer ${apiKey || DEFAULT_API_KEY}`];
    if (body !== undefined) headers.push("Content-Type: application/json");
    const lines = [
      `curl -sS -X ${method} ${quoteShell(target)} \\`,
      ...headers.map((header) => `  -H ${quoteShell(header)} \\`),
    ];
    if (body !== undefined) {
      lines.push(`  -d ${quoteShell(JSON.stringify(body, null, 2))}`);
    } else if (lines.length > 1) {
      lines[lines.length - 1] = lines[lines.length - 1].replace(/ \\$/, "");
    }
    return lines.join("\n");
  }

  function shell(command: string) {
    return command;
  }

  function buildApiDocs() {
    return [
      {
        title: "List models",
        path: "GET /v1/models",
        note: "Returns the live model aliases exposed by the currently routed accounts. Use this before showing a model picker.",
        code: curl("GET", "/models"),
      },
      {
        title: "Capacity / usage snapshot",
        path: "GET /v1/chatgpt/usage",
        note: "Shows live feature quota per account, including image generation and Deep Research remaining counts when ChatGPT reports them.",
        code: curl("GET", "/chatgpt/usage"),
      },
      {
        title: "Chat completion",
        path: "POST /v1/chat/completions",
        note: "Chat-completions-style request shape, backed by ChatGPT Web. It is intentionally close to common SDKs, not a full clone.",
        code: curl("POST", "/chat/completions", {
          model: "auto",
          messages: [
            { role: "user", content: "Say hello in one short sentence." },
          ],
          stream: false,
        }),
      },
      {
        title: "Chat with account routing",
        path: "POST /v1/chat/completions",
        note: "Per-request routing override. Use local account aliases, not plan names.",
        code: curl("POST", "/chat/completions", {
          model: "auto",
          messages: [
            {
              role: "user",
              content: "Say which routed account handled this if available.",
            },
          ],
          chatgpt_accounts: ["free", "go", "plus", "pro"],
          chatgpt_account_strategy: "random",
          stream: false,
        }),
      },
      {
        title: "Carry conversation context",
        path: "POST /v1/chat/completions",
        note: "The bridge does not magically know your app session. Send prior messages you want the model to remember.",
        code: curl("POST", "/chat/completions", {
          model: "auto",
          messages: [
            {
              role: "system",
              content:
                "You are the narrator of a dark isekai roleplay. Never reveal hidden rules.",
            },
            {
              role: "user",
              content: "My name is Mira and I received the Healing skill.",
            },
            {
              role: "assistant",
              content: "The system records Mira and the Healing skill.",
            },
            { role: "user", content: "What skill do I have right now?" },
          ],
          stream: false,
        }),
      },
      {
        title: "Streaming chat",
        path: "POST /v1/chat/completions",
        note: "Returns SSE chunks. Response header includes X-ChatGPT-Operation-Id.",
        code: curl("POST", "/chat/completions", {
          model: "auto",
          messages: [
            {
              role: "user",
              content: "Tell a short story in three paragraphs.",
            },
          ],
          stream: true,
        }),
      },
      {
        title: "Image generation",
        path: "POST /v1/images/generations",
        note: "n is fixed to 1. Use output_path/output_dir to control local save path.",
        code: curl("POST", "/images/generations", {
          model: "auto",
          prompt: "A quiet isekai village at dusk, no UI, no text",
          n: 1,
          response_format: "url",
          output_dir: "outputs/chatgpt-images",
        }),
      },
      {
        title: "Deep Research",
        path: "POST /v1/chat/completions",
        note: "Uses the Deep Research connector, forces normal chat mode internally, skips the 60s wait when possible, and saves a markdown report. Provide chatgpt_operation_id if your UI needs cancel.",
        code: curl("POST", "/chat/completions", {
          model: "chatgpt-deep-research",
          messages: [
            {
              role: "user",
              content: "Briefly research whether LLMs could reach AGI.",
            },
          ],
          chatgpt_operation_id: "chatgptop_research_demo",
          output_dir: "outputs/chatgpt-research",
        }),
      },
      {
        title: "Operation status",
        path: "GET /v1/chatgpt/operations/{id}",
        note: "Poll this before cancelling long jobs. For Deep Research, wait for deep_research_ready=true before expecting the MCP stop call to be immediate.",
        code: curl(
          "GET",
          "/chatgpt/operations/chatgptop_REPLACE_ME",
        ),
      },
      {
        title: "Cancel operation",
        path: "POST /v1/chatgpt/operations/{id}/cancel",
        note: "Use when the client aborts, closes a tab, or receives Ctrl-C. Deep Research cancel is best effort until operation status shows deep_research_ready=true.",
        code: curl(
          "POST",
          "/chatgpt/operations/chatgptop_REPLACE_ME/cancel",
          {},
        ),
      },
      {
        title: "Usage / remaining",
        path: "GET /v1/chatgpt/usage",
        note: "Live account limits and blocked feature windows.",
        code: curl("GET", "/chatgpt/usage"),
      },
      {
        title: "Check accounts",
        path: "POST /v1/chatgpt/admin/accounts/check",
        note: "Console API route. Checks all accounts or one named account.",
        code: curl("POST", "/chatgpt/admin/accounts/check", { account: "all" }),
      },
      {
        title: "Save runtime limits",
        path: "POST /v1/chatgpt/admin/settings/save",
        note: "Persists local bridge throttles in the admin SQLite DB and applies chat limits immediately.",
        code: curl("POST", "/chatgpt/admin/settings/save", {
          settings: {
            concurrency: normalizeConcurrency(concurrency),
          },
        }),
      },
      {
        title: "Save account capture",
        path: "POST /v1/chatgpt/admin/captures/save",
        note: "Validates required and recommended checks first, then writes the local account capture only if every check passes.",
        code: curl("POST", "/chatgpt/admin/captures/save", {
          account: "pro",
          capture_text: "PASTE REQUEST CAPTURE HERE",
        }),
      },
      {
        title: "Delete account",
        path: "POST /v1/chatgpt/admin/accounts/delete",
        note: "Deletes only local capture/settings metadata for the named account.",
        code: curl("POST", "/chatgpt/admin/accounts/delete", {
          account: "free",
          delete_capture: true,
          delete_settings: true,
        }),
      },
      {
        title: "Delete artifact",
        path: "POST /v1/chatgpt/admin/artifacts/delete",
        note: "delete_file=false removes the console DB record only.",
        code: curl("POST", "/chatgpt/admin/artifacts/delete", {
          file_id: "file_REPLACE_ME",
          delete_file: false,
        }),
      },
      {
        title: "Inject opencode",
        path: "POST /v1/chatgpt/admin/opencode/inject",
        note: "Writes opencode provider config from the current API target.",
        code: curl("POST", "/chatgpt/admin/opencode/inject", {
          base_url: baseUrl,
          api_key: apiKey || DEFAULT_API_KEY,
          model: "chatgpt-web/auto@optimized",
        }),
      },
    ];
  }

  function buildCliDocs() {
    return [
      {
        title: "Check running server",
        note: "Use this first inside Docker, SSH, or a headless box.",
        code: shell(
          `python3 -m chatgpt_api admin status --base-url ${quoteShell(baseUrl)} --api-key ${quoteShell(apiKey || DEFAULT_API_KEY)}`,
        ),
      },
      {
        title: "List live usage",
        note: "Fetches the same usage table as /chatgpt:usage without opening the web console.",
        code: shell(
          `python3 -m chatgpt_api admin usage --base-url ${quoteShell(baseUrl)} --api-key ${quoteShell(apiKey || DEFAULT_API_KEY)}`,
        ),
      },
      {
        title: "API health from CLI",
        note: "Read-only consumer check. This goes through the same /v1 server target as apps.",
        code: shell(
          `python3 -m chatgpt_api api health --base-url ${quoteShell(baseUrl)} --api-key ${quoteShell(apiKey || DEFAULT_API_KEY)}`,
        ),
      },
      {
        title: "API chat with routing",
        note: "Tests the real app route with per-request accounts and strategy overrides.",
        code: shell(
          `python3 -m chatgpt_api api chat --message 'Reply with exactly: bridge ok' --accounts free,go,plus,pro --account-strategy random --base-url ${quoteShell(baseUrl)} --api-key ${quoteShell(apiKey || DEFAULT_API_KEY)}`,
        ),
      },
      {
        title: "API image generation",
        note: "Calls /v1/images/generations and saves only completed image artifacts.",
        code: shell(
          `python3 -m chatgpt_api api image --prompt 'small blue app icon, no text' --output-dir ./outputs/manual-images --base-url ${quoteShell(baseUrl)} --api-key ${quoteShell(apiKey || DEFAULT_API_KEY)}`,
        ),
      },
      {
        title: "API research and cancel",
        note: "Start with a known operation id, poll until deep_research_ready=yes, then cancel from another terminal if needed.",
        code: shell(
          [
            `python3 -m chatgpt_api api research --prompt 'Briefly research whether LLMs could reach AGI.' --operation-id chatgptop_research_demo --base-url ${quoteShell(baseUrl)} --api-key ${quoteShell(apiKey || DEFAULT_API_KEY)}`,
            `python3 -m chatgpt_api api operation --operation-id chatgptop_research_demo --base-url ${quoteShell(baseUrl)} --api-key ${quoteShell(apiKey || DEFAULT_API_KEY)}`,
            `python3 -m chatgpt_api api cancel --operation-id chatgptop_research_demo --base-url ${quoteShell(baseUrl)} --api-key ${quoteShell(apiKey || DEFAULT_API_KEY)}`,
          ].join("\n"),
        ),
      },
      {
        title: "Set runtime limits",
        note: "Persists per-plan/per-account throttles into the admin SQLite DB.",
        code: shell(
          `python3 -m chatgpt_api admin set-limits --chat free=1,go=2,plus=3,pro=4 --upload free=1,go=1,plus=1,pro=1 --image free=1,go=1,plus=2,pro=3 --research free=1,go=1,plus=2,pro=2 --base-url ${quoteShell(baseUrl)} --api-key ${quoteShell(apiKey || DEFAULT_API_KEY)}`,
        ),
      },
      {
        title: "Save account capture",
        note: "Fails before writing if the copied request is incomplete. Use it to refresh an expired account capture safely.",
        code: shell(
          `python3 -m chatgpt_api admin save-capture --account plus-main --capture-file ./chatgpt-request.txt --base-url ${quoteShell(baseUrl)} --api-key ${quoteShell(apiKey || DEFAULT_API_KEY)}`,
        ),
      },
      {
        title: "Inject opencode",
        note: "Only writes opencode consumer config. It does not configure accounts, ports, or quotas.",
        code: shell(
          `python3 -m chatgpt_api admin opencode inject --model chatgpt-web/auto@optimized --base-url ${quoteShell(baseUrl)} --api-key ${quoteShell(apiKey || DEFAULT_API_KEY)}`,
        ),
      },
      {
        title: "Docker-style launch",
        note: "Use environment variables for container entrypoints and mounted volumes.",
        code: shell(
          [
            `CHATGPT_API_KEY=${quoteShell(apiKey || DEFAULT_API_KEY)} \\`,
            ...(serverAccounts.trim() ? [`CHATGPT_ACCOUNTS=${quoteShell(serverAccounts)} \\`] : []),
            `CHATGPT_API_HOST=0.0.0.0 \\`,
            `CHATGPT_API_PORT=${quoteShell(serverPort)} \\`,
            `CHATGPT_PUBLIC_BASE_URL=${quoteShell(serverPublicBase)} \\`,
            `CHATGPT_IMAGE_OUTPUT_DIR=${quoteShell(imageOutputDir)} \\`,
            `CHATGPT_RESEARCH_OUTPUT_DIR=${quoteShell(researchOutputDir)} \\`,
            `CHATGPT_UPLOAD_CONCURRENCY=free=1,go=1,plus=1,pro=1 \\`,
            `CHATGPT_RESEARCH_CONCURRENCY=free=1,go=1,plus=2,pro=2 \\`,
            `CHATGPT_ADMIN_DB_PATH=${quoteShell(adminDbPath)} \\`,
            `python3 -m chatgpt_api serve`,
          ].join("\n"),
        ),
      },
    ];
  }

  function buildResponseExamples() {
    const fileBase = baseUrl.replace(/\/+$/, "");
    return [
      {
        title: "Chat response",
        body: "Normal chat reads choices[0].message.content. The bridge also includes the account that handled the call.",
        code: JSON.stringify(
          {
            id: "chatcmpl_local_example",
            object: "chat.completion",
            model: "auto",
            chatgpt_account: "pro",
            choices: [
              {
                index: 0,
                message: {
                  role: "assistant",
                  content: "The local bridge is working.",
                },
                finish_reason: "stop",
              },
            ],
          },
          null,
          2,
        ),
      },
      {
        title: "Model fallback response",
        body: "When a selected model is limited and fallback is enabled, surface this to users instead of pretending nothing happened.",
        code: JSON.stringify(
          {
            id: "chatcmpl_local_fallback",
            object: "chat.completion",
            model: "auto",
            chatgpt_account: "free",
            chatgpt_requested_model: "gpt-5-5",
            chatgpt_fallback_model: "auto",
            choices: [
              {
                message: {
                  role: "assistant",
                  content: "I switched to an available model for this account.",
                },
              },
            ],
          },
          null,
          2,
        ),
      },
      {
        title: "Image response",
        body: "Use download_url for browser/LAN clients. path is only useful on the same machine that runs the API.",
        code: JSON.stringify(
          {
            created: 1782320000,
            chatgpt_account: "pro",
            chatgpt_operation_id: "chatgptop_image_example",
            data: [
              {
                url: `${fileBase}/chatgpt/files/file_cat/cat.png`,
                download_url: `${fileBase}/chatgpt/files/file_cat/cat.png`,
                path: "/Users/work/Desktop/chatgpt-api/outputs/chatgpt-images/cat.png",
                filename: "cat.png",
                content_type: "image/png",
              },
            ],
          },
          null,
          2,
        ),
      },
      {
        title: "Image edit response",
        body: "Edits and multi-image composites return one completed image. Use warnings to tell users why source aspect ratio matters.",
        code: JSON.stringify(
          {
            created: 1782320100,
            chatgpt_account: "pro",
            chatgpt_operation_id: "chatgptop_edit_example",
            input_image_count: 2,
            aspect_ratio: "1:1",
            warnings: [
              "Image edits preserve layout best when the source image already matches one of 1:1, 3:4, 9:16, 4:3, or 16:9.",
            ],
            data: [
              {
                url: `${fileBase}/chatgpt/files/file_edit/edited.png`,
                download_url: `${fileBase}/chatgpt/files/file_edit/edited.png`,
                path: "/Users/work/Desktop/chatgpt-api/outputs/chatgpt-images/edited.png",
                filename: "edited.png",
                content_type: "image/png",
              },
            ],
          },
          null,
          2,
        ),
      },
      {
        title: "Vision/OCR response",
        body: "Vision returns assistant text in text and choices[0].message.content. Ask for plain OCR, markdown, or strict JSON with estimated bbox coordinates in the prompt.",
        code: JSON.stringify(
          {
            id: "chatcmpl_vision_example",
            object: "chatgpt.vision",
            model: "auto",
            chatgpt_account: "pro",
            mode: "ocr",
            input_image_count: 1,
            text: JSON.stringify({
              items: [
                {
                  text: "FW",
                  bbox: { x: 412, y: 390, w: 210, h: 116 },
                  confidence: "medium",
                },
              ],
            }),
            choices: [
              {
                message: {
                  role: "assistant",
                  content: JSON.stringify({
                    items: [
                      {
                        text: "FW",
                        bbox: { x: 412, y: 390, w: 210, h: 116 },
                        confidence: "medium",
                      },
                    ],
                  }),
                },
              },
            ],
            note: "Bbox values are model-estimated. Use a dedicated OCR/layout engine when exact coordinates matter.",
          },
          null,
          2,
        ),
      },
      {
        title: "Deep Research response",
        body: "The chat content should be short. The useful artifact is the saved markdown report and its download URL.",
        code: JSON.stringify(
          {
            id: "chatcmpl_research_example",
            object: "chat.completion",
            model: "chatgpt-deep-research",
            chatgpt_account: "pro",
            chatgpt_research_report_path:
              "/Users/work/Desktop/chatgpt-api/outputs/chatgpt-research/agi-report.md",
            chatgpt_research_report_download_url: `${fileBase}/chatgpt/files/file_report/agi-report.md`,
            choices: [
              {
                message: {
                  role: "assistant",
                  content:
                    "Deep Research finished. Saved report: agi-report.md",
                },
              },
            ],
          },
          null,
          2,
        ),
      },
    ];
  }

  function buildRouteResponseDocs() {
    const fileBase = baseUrl.replace(/\/+$/, "");
    return [
      {
        route: "POST /v1/chat/completions",
        kind: "chat",
        read: "choices[0].message.content",
        files: "none",
        operation: "X-ChatGPT-Operation-Id header when available",
        response: {
          id: "chatcmpl_local",
          object: "chat.completion",
          model: "auto",
          chatgpt_account: "pro",
          choices: [{ message: { role: "assistant", content: "..." } }],
        },
      },
      {
        route: "POST /v1/chat/completions stream=true",
        kind: "streaming chat",
        read: "SSE data chunks: choices[0].delta.content",
        files: "none",
        operation:
          "Save X-ChatGPT-Operation-Id; call cancel route if user aborts",
        response: {
          event: "data",
          payload: {
            object: "chat.completion.chunk",
            choices: [{ delta: { content: "partial text" } }],
          },
        },
      },
      {
        route: "POST /v1/chat/completions with tools",
        kind: "opencode agent",
        read: "choices[0].message.tool_calls",
        files: "client executes tools; bridge never writes workspace files",
        operation: "finish_reason=tool_calls",
        response: {
          choices: [
            {
              finish_reason: "tool_calls",
              message: {
                role: "assistant",
                content: null,
                tool_calls: [
                  {
                    type: "function",
                    function: {
                      name: "apply_patch",
                      arguments: "{\"patchText\":\"...\"}",
                    },
                  },
                ],
              },
            },
          ],
        },
      },
      {
        route: "POST /v1/images/generations",
        kind: "image",
        read: "data[0].download_url or data[0].url",
        files: "Saved only after a real image asset is returned",
        operation: "chatgpt_operation_id in JSON and response header",
        response: {
          created: 1782320000,
          chatgpt_account: "pro",
          chatgpt_operation_id: "chatgptop_image",
          data: [
            {
              url: `${fileBase}/chatgpt/files/file_cat/cat.png`,
              download_url: `${fileBase}/chatgpt/files/file_cat/cat.png`,
              path: "/local/outputs/chatgpt-images/cat.png",
              filename: "cat.png",
            },
          ],
        },
      },
      {
        route: "POST /v1/images/edits",
        kind: "image edit / composite",
        read: "data[0].download_url or data[0].path",
        files:
          "One completed output image saved after source images are uploaded",
        operation: "chatgpt_operation_id in JSON and response header",
        response: {
          created: 1782320100,
          chatgpt_account: "pro",
          chatgpt_operation_id: "chatgptop_edit",
          input_image_count: 2,
          aspect_ratio: "1:1",
          warnings: [
            "Image edits preserve layout best when the source image already matches one of 1:1, 3:4, 9:16, 4:3, or 16:9.",
          ],
          data: [
            {
              url: `${fileBase}/chatgpt/files/file_edit/edited.png`,
              download_url: `${fileBase}/chatgpt/files/file_edit/edited.png`,
              path: "/local/outputs/chatgpt-images/edited.png",
              filename: "edited.png",
            },
          ],
        },
      },
      {
        route: "POST /v1/chatgpt/vision",
        kind: "OCR / image understanding",
        read: "text or choices[0].message.content; can be plain text or prompt-shaped JSON",
        files: "none; source images are temporary request inputs",
        operation: "Uses upload + chat concurrency buckets",
        response: {
          id: "chatcmpl_vision",
          object: "chatgpt.vision",
          model: "auto",
          chatgpt_account: "pro",
          mode: "describe",
          input_image_count: 1,
          text: "A square app icon with stylized letters. For OCR bbox mode, ask for strict JSON in the prompt.",
          choices: [
            {
              message: {
                role: "assistant",
                content: "A square app icon with stylized letters. For OCR bbox mode, ask for strict JSON in the prompt.",
              },
            },
          ],
        },
      },
      {
        route: "POST /v1/chat/completions deep research",
        kind: "research",
        read: "chatgpt_research_report_download_url",
        files: "Markdown report saved in research output dir",
        operation:
          "Long job; cancel with operation id. WSS discovers the widget session, then MCP stop is sent.",
        response: {
          id: "chatcmpl_research",
          chatgpt_operation_id: "chatgptop_research_demo",
          chatgpt_research_report_path: "/local/outputs/research/report.md",
          chatgpt_research_report_download_url: `${fileBase}/chatgpt/files/file_report/report.md`,
          choices: [
            {
              message: {
                content: "Deep Research finished. Saved report: report.md",
              },
            },
          ],
        },
      },
      {
        route: "GET/HEAD /v1/chatgpt/files/{file_id}/{filename}",
        kind: "download",
        read: "binary file body",
        files: "Works for images and markdown reports",
        operation: "Use public_base_url for LAN clients. URLs restore from the admin DB after API restart when the file still exists.",
        response: "GET returns the file body. HEAD returns Content-Type, Content-Length, and Content-Disposition without a body.",
      },
      {
        route: "GET /v1/chatgpt/operations/{id}",
        kind: "operation",
        read: "selected account, conversation id, Deep Research readiness, cancel state",
        files: "none",
        operation:
          "Poll this before manual Deep Research cancel; deep_research_ready=true means the widget session id is known.",
        response: {
          object: "chatgpt.operation",
          operation: {
            id: "chatgptop_REPLACE_ME",
            kind: "research",
            account: "pro",
            provider_selected: true,
            conversation_id: "conversation-id",
            deep_research_ready: true,
            pending_reason: null,
            cancel_requested: false,
            completed: false,
          },
        },
      },
      {
        route: "POST /v1/chatgpt/operations/{id}/cancel",
        kind: "cancel",
        read: "ok + cancel result",
        files: "no new file should be registered after cancel",
        operation:
          "Call from Ctrl-C, tab close, AbortController, or UI cancel. For Deep Research, poll operation first when possible.",
        response: {
          status: "ok",
          operation: {
            id: "chatgptop_REPLACE_ME",
            kind: "research",
            deep_research_ready: true,
            cancel_requested: true,
            completed: false,
            last_cancel_result: { conversation: { status: "ok" } },
          },
        },
      },
      {
        route: "GET /v1/chatgpt/usage",
        kind: "quota",
        read: "accounts[].features.{file_upload,image_gen,deep_research}",
        files: "none",
        operation: "Use before expensive upload/image/research jobs",
        response: {
          object: "chatgpt.usage",
          accounts: [
            {
              account: "pro",
              ok: true,
              features: {
                file_upload: { remaining: 23 },
                image_gen: { remaining: 938, reset_after: "2026-06-25T00:37:00Z" },
                deep_research: { remaining: 224 },
              },
            },
          ],
        },
      },
      {
        route: "GET /v1/models",
        kind: "models",
        read: "data[].id",
        files: "none",
        operation: "Free/Go should choose auto only",
        response: {
          object: "list",
          data: [
            { id: "auto", object: "model" },
            { id: "gpt-5-5", object: "model" },
            { id: "gpt-5-5-thinking-extended", object: "model" },
            { id: "gpt-5-5-pro-standard", object: "model" },
            { id: "auto@optimized", object: "model" },
            { id: "gpt-image-1", object: "model" },
            { id: "chatgpt-deep-research", object: "model" },
          ],
        },
      },
      {
        route: "GET /v1/chatgpt/admin/status",
        kind: "admin",
        read: "server/routing/storage/settings",
        files: "none",
        operation: "Console health and launch state",
        response: {
          object: "chatgpt.admin.status",
          ok: true,
          server: { base_url: baseUrl },
          routing: { accounts: ["pro", "free"] },
          storage: { artifact_count: 2 },
        },
      },
      {
        route: "POST /v1/chatgpt/admin/captures/save",
        kind: "admin account",
        read: "validation result, saved=true",
        files: "writes local capture only after validation passes",
        operation: "Use Update capture when token/cookie expires",
        response: {
          object: "chatgpt.admin.capture",
          ok: true,
          saved: true,
          account: "pro",
          checks: [{ name: "authorization", ok: true }],
        },
      },
      {
        route: "POST /v1/chatgpt/admin/accounts/delete",
        kind: "admin account",
        read: "deleted flags",
        files: "deletes local capture/settings metadata only",
        operation: "Use before replacing a broken account",
        response: {
          object: "chatgpt.admin.account.delete",
          ok: true,
          account: "free",
          deleted_capture: true,
        },
      },
      {
        route: "POST /v1/chatgpt/admin/settings/save",
        kind: "admin settings",
        read: "settings",
        files: "writes admin SQLite DB",
        operation: "Persists local concurrency and warning settings",
        response: {
          object: "chatgpt.admin.settings",
          ok: true,
          settings: { concurrency: DEFAULT_CONCURRENCY },
        },
      },
      {
        route: "GET /v1/chatgpt/admin/artifacts",
        kind: "admin storage",
        read: "artifacts[].download_url",
        files: "shows completed files only",
        operation: "Use for gallery/storage pages",
        response: {
          object: "chatgpt.admin.artifacts",
          artifacts: [
            {
              file_id: "file_cat",
              filename: "cat.png",
              exists: true,
              download_url: `${fileBase}/chatgpt/files/file_cat/cat.png`,
            },
          ],
        },
      },
      {
        route: "POST /v1/chatgpt/admin/artifacts/delete",
        kind: "admin storage",
        read: "deleted flags",
        files: "delete_file=true also removes the local file",
        operation: "Use for storage cleanup",
        response: {
          object: "chatgpt.admin.artifact.delete",
          ok: true,
          deleted_record: true,
          deleted_file: false,
        },
      },
      {
        route: "POST /v1/chatgpt/admin/opencode/inject",
        kind: "admin opencode",
        read: "injected status and config path",
        files: "writes opencode config only",
        operation: "Does not configure accounts or start the server",
        response: {
          object: "chatgpt.admin.opencode",
          ok: true,
          injected: true,
          config_path: "~/.config/opencode/opencode.json",
        },
      },
      {
        route: "POST /v1/chatgpt/admin/opencode/eject",
        kind: "admin opencode",
        read: "ejected status",
        files: "removes generated provider block",
        operation: "Use to restore normal opencode provider config",
        response: {
          object: "chatgpt.admin.opencode",
          ok: true,
          action: "eject",
        },
      },
    ];
  }

  function buildDownloadGuides() {
    const localBase = baseUrl || DEFAULT_BASE_URL;
    const lanBase = serverPublicBase.includes("YOUR-LAN-IP")
      ? "http://192.168.1.203:8000/v1"
      : serverPublicBase;
    return [
      {
        title: "Same machine",
        body: "Use path when your app runs on the same machine as the API server. This is fastest for local scripts.",
        example:
          "/Users/work/Desktop/chatgpt-api/outputs/chatgpt-images/cat.png",
      },
      {
        title: "Browser or LAN client",
        body: "Use download_url. Never send a macOS path to another computer; it cannot read your filesystem.",
        example: `${localBase.replace(/\/+$/, "")}/chatgpt/files/file_cat/cat.png`,
      },
      {
        title: "LAN public URL",
        body: "Start the API with --host 0.0.0.0 and set --public-base-url to the LAN IP. Returned download_url will then be reachable by phones or other computers.",
        example: `python3 -m chatgpt_api serve --host 0.0.0.0 --public-base-url ${quoteShell(lanBase)}`,
      },
      {
        title: "Research reports",
        body: "Deep Research saves markdown into the research output directory and registers it in the same file download API as images.",
        example: `${localBase.replace(/\/+$/, "")}/chatgpt/files/file_report/agi-report.md`,
      },
    ];
  }

  function buildServeCommand() {
    const args: string[] = [
      "python3 -m chatgpt_api serve",
      "--account-strategy",
      quoteShell(serverStrategy),
      "--host",
      quoteShell(serverHost),
      "--port",
      quoteShell(serverPort),
      "--api-key",
      quoteShell(serverKey),
      "--public-base-url",
      quoteShell(serverPublicBase),
      "--image-output-dir",
      quoteShell(imageOutputDir),
      "--research-output-dir",
      quoteShell(researchOutputDir),
      "--admin-db-path",
      quoteShell(adminDbPath),
      "--web-timeout",
      quoteShell(webTimeout),
      "--chat-concurrency",
      quoteShell(concurrencyArg("chat")),
      "--upload-concurrency",
      quoteShell(concurrencyArg("upload")),
      "--image-concurrency",
      quoteShell(concurrencyArg("image")),
      "--research-concurrency",
      quoteShell(concurrencyArg("research")),
      "--agent-mode",
      quoteShell(agentMode),
      "--model-fallback",
      quoteShell(modelFallback),
      temporaryChat ? "--temporary-chat" : "--normal-chat",
    ];
    if (!serverAccounts.trim() && serverAccount.trim()) {
      args.splice(1, 0, "--account", quoteShell(serverAccount));
    }
    if (serverAccounts.trim()) {
      args.splice(1, 0, "--accounts", quoteShell(serverAccounts));
    }
    return args.join(" ");
  }

  async function copyText(text: string) {
    await navigator.clipboard.writeText(text);
    showToast("Copied");
  }

  function quoteShell(value: string) {
    return `'${String(value).replaceAll("'", "'\\''")}'`;
  }

  function featureText(feature: Json | undefined) {
    if (!feature) return "not reported";
    if (feature.blocked) {
      return `blocked${feature.reset_after || feature.resets_after ? ` · reset ${feature.reset_after || feature.resets_after}` : ""}`;
    }
    if (feature.remaining !== undefined && feature.remaining !== null) {
      return `${feature.remaining}${feature.reset_after ? ` · reset ${feature.reset_after}` : ""}`;
    }
    return String(feature.status || "not reported");
  }

  function routedAccountNames(): string[] {
    const routed = Array.isArray(routing.accounts)
      ? (routing.accounts as unknown[])
          .map((account: unknown) => String(account))
          .filter(Boolean)
      : [];
    const selected = selectedServerAccounts.length
      ? selectedServerAccounts
      : splitCsv(serverAccounts);
    const fallback = accounts.map((account) => account.account);
    return Array.from(
      new Set((routed.length ? routed : selected.length ? selected : fallback).filter(Boolean)),
    );
  }

  function usageForAccount(account: string) {
    return (
      usageAccounts.find((item: Json) => String(item.account) === account) ??
      liveByAccount[account] ??
      {}
    );
  }

  function accountRowFor(account: string) {
    return accounts.find((row) => row.account === account);
  }

  function normalizedPlan(value: unknown) {
    const raw = String(value ?? "").toLowerCase();
    if (raw.includes("pro")) return "pro";
    if (raw.includes("plus")) return "plus";
    if (raw.includes("go")) return "go";
    return "free";
  }

  function capabilityPlan(account: string) {
    const usageAccount = usageForAccount(account);
    const row = accountRowFor(account);
    const planType = usageAccount.plan_type ?? row?.plan_type;
    if (planType) return normalizedPlan(planType);
    const bucket = usageAccount.plan_bucket ?? row?.plan_bucket;
    if (String(bucket ?? "").toLowerCase().includes("paid")) {
      return planForAccount(account);
    }
    return normalizedPlan(bucket ?? planForAccount(account));
  }

  function featureKey(feature: string) {
    if (feature === "upload") return "file_upload";
    if (feature === "image") return "image_gen";
    if (feature === "research") return "deep_research";
    return "chat";
  }

  function featureLimit(feature: string, account: string) {
    const routeLimit =
      feature === "chat"
        ? routing.account_concurrency?.[account]
        : routing.feature_concurrency?.[feature]?.[account];
    const routeNumber = Number(routeLimit);
    if (Number.isFinite(routeNumber)) return Math.max(0, routeNumber);
    const current = normalizeConcurrency(concurrency);
    const accountLimit = Number(current?.[feature]?.accounts?.[account]);
    if (Number.isFinite(accountLimit)) return Math.max(0, accountLimit);
    const plan = capabilityPlan(account);
    return Number(current?.[feature]?.plans?.[plan] ?? 0);
  }

  function featureQuota(feature: string) {
    if (feature === "chat") {
      return usageCheckedAt
        ? "model limits only when ChatGPT reports them"
        : "run usage refresh for live model limits";
    }
    const key = featureKey(feature);
    const rows = routedAccountNames()
      .map((account) => usageForAccount(account))
      .filter(Boolean);
    if (!rows.length || !usageCheckedAt) return "usage not loaded";
    let remaining = 0;
    let remainingSeen = false;
    let blocked = 0;
    const resets: string[] = [];
    for (const row of rows) {
      const item = row.features?.[key];
      if (!item) continue;
      if (item.blocked) blocked += 1;
      const value = Number(item.remaining);
      if (Number.isFinite(value)) {
        remaining += value;
        remainingSeen = true;
      }
      const reset = item.reset_after || item.resets_after;
      if (reset) resets.push(String(reset));
    }
    const resetText = resets.length ? ` · next reset ${formatReset(resets[0])}` : "";
    if (remainingSeen) {
      return `${remaining} remaining${blocked ? ` · ${blocked} blocked` : ""}${resetText}`;
    }
    if (blocked) return `${blocked} account(s) blocked${resetText}`;
    return "not reported";
  }

  function formatReset(value: string) {
    const parsed = new Date(value);
    if (Number.isNaN(parsed.valueOf())) return value;
    return parsed.toLocaleString();
  }

  function capacityTone(
    feature: string,
    parallel: number,
    quota: string,
  ): "ok" | "warn" | "bad" | "muted" {
    if (!status) return "muted";
    if (parallel <= 0) return feature === "research" ? "warn" : "bad";
    if (quota.includes("blocked") && !/\d+ remaining/.test(quota)) return "bad";
    if (quota.includes("not loaded")) return "warn";
    return "ok";
  }

  function capacityAccountDetail(feature: string, account: string) {
    const live = usageForAccount(account);
    if (feature === "chat") {
      return live.default_model_slug
        ? `default ${live.default_model_slug}`
        : "default model not reported";
    }
    return featureText(live.features?.[featureKey(feature)]);
  }

  function capacityAccountTone(
    feature: string,
    account: string,
    limit: number,
  ): "ok" | "warn" | "bad" | "muted" {
    const live = usageForAccount(account);
    const item = live.features?.[featureKey(feature)];
    if (live.ok === false) return "bad";
    if (limit <= 0) return "muted";
    if (item?.blocked) return "bad";
    if (live.ok) return "ok";
    return "warn";
  }

  function buildCapacityCards(): CapacityCard[] {
    const names = routedAccountNames();
    const specs = [
      {
        id: "chat",
        label: "Chat",
        title: "Realtime chat and app narration",
        route: "POST /v1/chat/completions",
        model: "auto, gpt-5-5*, and agent suffixes from /v1/models",
        output: "No file output",
        note: "Best path for game chat, roleplay state updates, streaming UI, and opencode tool calls.",
      },
      {
        id: "upload",
        label: "Upload",
        title: "Vision, OCR, and source-image edits",
        route: "POST /v1/chatgpt/vision · POST /v1/images/edits",
        model: "auto for OCR/describe, gpt-image-1 for edits",
        output:
          "Vision returns assistant text or prompt-shaped JSON; edits save one image into " +
          String(storage.image_output_dir ?? imageOutputDir),
        note: "Up to 10 source images per request. This consumes file_upload when reported. OCR bbox JSON is model-estimated; editing/compositing returns exactly one final image.",
      },
      {
        id: "image",
        label: "Image",
        title: "Image generation",
        route: "POST /v1/images/generations",
        model: "gpt-image-1 or auto",
        output: String(storage.image_output_dir ?? imageOutputDir),
        note: "Completed images are registered in storage and returned with a download_url for browser or LAN clients.",
      },
      {
        id: "research",
        label: "Research",
        title: "Deep Research report jobs",
        route: "POST /v1/chat/completions",
        model: "chatgpt-deep-research",
        output: String(storage.research_output_dir ?? researchOutputDir),
        note: "Forces normal chat mode, attempts skip_sleep, and saves the final markdown report as an artifact.",
      },
    ];
    return specs.map((spec) => {
      const accountsForFeature = names.map((name) => {
        const limit = featureLimit(spec.id, name);
        return {
          name,
          plan: capabilityPlan(name),
          limit,
          status:
            spec.id === "chat"
              ? accountHealthLabel({
                  ...(accountRowFor(name) ?? { account: name }),
                  live: liveByAccount[name],
                })
              : limit > 0
                ? "enabled"
                : "off",
          detail: capacityAccountDetail(spec.id, name),
          tone: capacityAccountTone(spec.id, name, limit),
        };
      });
      const parallel = accountsForFeature.reduce(
        (total, account) => total + account.limit,
        0,
      );
      const quota = featureQuota(spec.id);
      return {
        ...spec,
        parallel,
        quota,
        accounts: accountsForFeature,
        tone: capacityTone(spec.id, parallel, quota),
      };
    });
  }

  function fallbackModelIds() {
    return [
      "auto",
      "gpt-5-5",
      "gpt-5-5-thinking-standard",
      "gpt-5-5-thinking-extended",
      "gpt-5-5-thinking-max",
      "gpt-5-5-pro-standard",
      "gpt-5-5-pro-extended",
      "auto@optimized",
      "auto@opencode",
      "gpt-image-1",
      "chatgpt-deep-research",
    ];
  }

  function modelHelp(id: string) {
    if (id === "auto") return "Safe default. Free/Go should stay here.";
    if (id === "gpt-image-1") return "Image generation alias for /v1/images/generations.";
    if (id.includes("deep-research")) return "Deep Research connector alias; long job with markdown output.";
    if (id.includes("@optimized")) return "Compact agent bridge prompt for opencode-style tools.";
    if (id.includes("@opencode")) return "Fuller opencode prompt bridge for tool use.";
    if (id.includes("thinking")) return "Paid thinking model alias; effort is encoded in the model id.";
    if (id.includes("-pro-")) return "Pro mode alias; requires a Pro-capable account.";
    if (id === "gpt-5-5") return "Explicit GPT-5.5 web model when the account supports it.";
    return "Discovered from the current routed account captures.";
  }

  function buildModelGroups(): ModelGroup[] {
    const ids = visibleModelIds;
    const normal = ids.filter(
      (id) =>
        !id.includes("@") &&
        !id.includes("image") &&
        !id.includes("research"),
    );
    const agent = ids.filter((id) => id.includes("@"));
    const image = ids.filter((id) => id.includes("image"));
    const research = ids.filter((id) => id.includes("research"));
    return [
      {
        id: "normal",
        title: "Chat models",
        note: "Use these with /v1/chat/completions. Free/Go should use auto.",
        ids: normal,
      },
      {
        id: "agent",
        title: "Agent prompt modes",
        note: "@optimized is compact. @opencode keeps more of the original opencode tool prompt.",
        ids: agent,
      },
      {
        id: "image",
        title: "Image model",
        note: "Use with /v1/images/generations. n is fixed to 1.",
        ids: image,
      },
      {
        id: "research",
        title: "Research model",
        note: "Use for Deep Research. It is a long job and saves a markdown artifact.",
        ids: research,
      },
    ].filter((group) => group.ids.length);
  }

  function formatBytes(value?: number) {
    if (!Number.isFinite(value)) return "-";
    if ((value ?? 0) < 1024) return `${value} B`;
    if ((value ?? 0) < 1024 * 1024)
      return `${((value ?? 0) / 1024).toFixed(1)} KB`;
    return `${((value ?? 0) / 1024 / 1024).toFixed(1)} MB`;
  }

  function downloadUrl(url: string) {
    if (!url) return "";
    return new URL(url, baseUrl.replace(/\/v1\/?$/, "")).href;
  }

  function isImageArtifact(artifact: Artifact) {
    return (
      artifact.kind === "image" ||
      artifact.content_type?.startsWith("image/") ||
      /\.(png|jpe?g|webp|gif)$/i.test(artifact.filename)
    );
  }

  function accountHealthLabel(row: AccountRow & { live?: LiveAccount }) {
    if (row.live?.ok) return "healthy";
    if (row.live?.ok === false) return "failed";
    if (row.configured || row.capture_exists) return "ready";
    return "missing";
  }

  function accountHealthTone(row: AccountRow & { live?: LiveAccount }) {
    const label = accountHealthLabel(row);
    if (label === "healthy") return "ok";
    if (label === "failed") return "bad";
    if (label === "ready") return "warn";
    return "muted";
  }

  function routeStatus(statusText: string) {
    if (statusText === "OK") return "ok";
    if (statusText === "FAIL") return "bad";
    return "warn";
  }

  function buildRouteMonitorRows() {
    const online = Boolean(status);
    const latency = apiLatencyMs ? `${apiLatencyMs}ms` : "-";
    const baseSuccess = online ? (failedAccountCount ? "degraded" : "ready") : "not checked";
    return [
      {
        path: "/health",
        status: online ? "OK" : "WARN",
        latency,
        success: online ? "live" : "pending",
      },
      {
        path: "/v1/chat/completions",
        status: online ? "OK" : "WARN",
        latency: online ? "stream capable" : "-",
        success: baseSuccess,
      },
      {
        path: "/v1/images/generations",
        status: online ? "OK" : "WARN",
        latency: online ? "async job" : "-",
        success: storage.artifact_count !== undefined ? `${storage.artifact_count} artifacts` : baseSuccess,
      },
      {
        path: "/v1/chatgpt/usage",
        status: online && activeAccountCount ? "OK" : "WARN",
        latency: usageCheckedAt || (online ? "account scan" : "-"),
        success: usageCheckedAt ? "live quota" : `${activeAccountCount} configured`,
      },
      {
        path: "/v1/models",
        status: online ? "OK" : "WARN",
        latency: modelsCheckedAt || "-",
        success: modelIds.length ? `${modelIds.length} models` : "not loaded",
      },
      {
        path: "/v1/chatgpt/files/{id}/{name}",
        status: online ? "OK" : "WARN",
        latency: "GET/HEAD",
        success: `${artifacts.length} indexed`,
      },
    ];
  }

  function buildEventRows() {
    const rows = [
      {
        tone: systemState === "offline" ? "bad" : "ok",
        title: systemState === "online" ? "API status refreshed" : "API status pending",
        detail: lastHealthCheckAt
          ? `Last health check at ${lastHealthCheckAt}`
          : "Run Health Check to populate live status.",
      },
      {
        tone: failedAccountCount ? "bad" : checkedAccountCount ? "ok" : "warn",
        title: failedAccountCount
          ? "Account errors detected"
          : checkedAccountCount
            ? "Account route clean"
            : "Account checks pending",
        detail: failedAccountCount
          ? `${failedAccountCount} account(s) returned live errors.`
          : checkedAccountCount
            ? `${checkedAccountCount} account(s) checked successfully.`
            : `${activeAccountCount} account(s) configured for routing.`,
      },
      {
        tone: "muted",
        title: "Artifact index",
        detail: `${artifacts.length} completed file record(s) loaded from storage.`,
      },
    ];
    if (lastError) {
      rows.unshift({
        tone: "bad",
        title: "Latest API error",
        detail: lastError,
      });
    }
    return rows;
  }

  async function launchBridge() {
    await copyText(serveCommand);
    setPage("settings");
    showToast("Launch command copied. Run it in Terminal, then refresh health.");
  }

  function buildCommandActions() {
    return [
      {
        id: "launch",
        title: "Launch bridge",
        detail: "Copy the full API serve command and open launch settings.",
        run: () => void launchBridge(),
      },
      {
        id: "health",
        title: "Run health check",
        detail: "Refresh status, accounts, routes, and artifact index.",
        run: () => void refreshAll(),
      },
      {
        id: "accounts",
        title: "Check accounts",
        detail: "Call the account checker for every configured capture.",
        run: () => void checkAccounts(),
      },
      {
        id: "strategy",
        title: "Switch strategy",
        detail: "Open runtime settings and update the account routing mode.",
        run: () => setPage("settings"),
      },
      {
        id: "docs",
        title: "Open API docs",
        detail: "Inspect request bodies, response contracts, files, and cancel routes.",
        run: () => setPage("api-docs"),
      },
    ];
  }

  function runCommandAction(action: { run: () => void }) {
    commandPaletteOpen = false;
    commandQuery = "";
    action.run();
  }

  function buildOpencodeProviderJson() {
    return JSON.stringify(
      {
        provider: {
          chatgptWeb: {
            npm: "@ai-sdk/openai-compatible",
            name: "ChatGPT Web Bridge",
            options: {
              baseURL: baseUrl,
              apiKey: apiKey || DEFAULT_API_KEY,
            },
            models: {
              "chatgpt-web/auto@optimized": {
                name: "ChatGPT Web Auto (optimized)",
              },
            },
          },
        },
      },
      null,
      2,
    );
  }
</script>

<div class="console-shell text-slate-100">
  <header class="console-topbar">
    <div class="console-brand">
      <div class="brand-mark">WB</div>
      <div class="min-w-0">
        <p class="brand-kicker">bridge control plane</p>
        <h1>Web Bridge Console</h1>
      </div>
    </div>

    <div class="api-command">
      <span
        class={`api-signal ${
          status ? "is-online" : lastError ? "is-error" : "is-waiting"
        }`}
      ></span>
      <div class="min-w-0">
        <div class="api-label">
          <strong>{systemLabel}</strong>
          <span>{String(routing.account_strategy ?? serverStrategy)}</span>
        </div>
        <div class="api-meta">
          <code>{lastError || baseUrl}</code>
          <code>{apiKey || DEFAULT_API_KEY}</code>
        </div>
      </div>
    </div>

    <div class="top-actions">
      <button class="top-action primary" onclick={launchBridge}>
        Launch Bridge
      </button>
      <button class="top-action" onclick={refreshAll}>Refresh</button>
      <button class="top-action" onclick={() => checkAccounts()}>
        Health Check
      </button>
      <button
        class="top-action ghost"
        onclick={() => {
          commandPaletteOpen = true;
          commandQuery = "";
        }}
      >
        Cmd+K
      </button>
    </div>
  </header>

  <nav class="module-strip" aria-label="Console sections">
    {#each pages as [id, label, description] (id)}
      <button
        class={`module-tab ${page === id ? "is-active" : ""}`}
        onclick={() => setPage(id)}
      >
        <span>{label}</span>
        <small>{description}</small>
      </button>
    {/each}
  </nav>

  <main class="console-main">
    <section class="system-status-strip" aria-label="System status">
      <div class={`status-word ${systemState}`}>
        <span></span>
        {systemLabel}
      </div>
      <div class="status-segment">
        <span>Latency</span>
        <strong>{apiLatencyMs ? `${apiLatencyMs}ms` : "-"}</strong>
      </div>
      <div class="status-segment">
        <span>Accounts</span>
        <strong>{activeAccountCount}</strong>
      </div>
      <div class="status-segment">
        <span>Healthy</span>
        <strong>{healthyAccountText}</strong>
      </div>
      <div class="status-segment">
        <span>Artifacts</span>
        <strong>{String(storage.artifact_count ?? artifacts.length)}</strong>
      </div>
      <div class="status-segment wide">
        <span>Runtime strategy</span>
        <strong>{String(routing.account_strategy ?? serverStrategy)}</strong>
      </div>
    </section>

    {#if page !== "overview"}
      <section class="page-command">
        <div class="min-w-0">
          <p class="page-kicker">module / {currentPage[0]}</p>
          <h2>{currentPage[1]}</h2>
          <p>{currentPage[2]}</p>
        </div>
      </section>
    {/if}

      {#if page === "overview"}
        <section class="dashboard-grid">
          <div class="control-plane" aria-label="Control plane">
            <article class="module-panel runtime-panel">
              <div class="panel-heading">
                <div>
                  <p class="panel-kicker">control plane</p>
                  <h2>Bridge Runtime</h2>
                </div>
                <span class={`panel-state ${systemState}`}>{systemLabel}</span>
              </div>

              <div class="runtime-command">
                <div>
                  <p>Active listener</p>
                  <code>{serverHost}:{serverPort}</code>
                </div>
                <button class="primary-cta" onclick={() => setPage("settings")}>
                  OPEN LAUNCH CONFIG
                </button>
              </div>

              <p class="runtime-note">
                This panel is live status only. Host, port, routing strategy,
                public URL, and bearer key apply when the API server starts;
                change them in Launch and restart the bridge.
              </p>

              <div class="runtime-readonly-grid">
                <div>
                  <span>Strategy</span>
                  <strong>{String(routing.account_strategy ?? serverStrategy)}</strong>
                </div>
                <div>
                  <span>Base URL</span>
                  <code>{baseUrl}</code>
                </div>
                <div>
                  <span>Dev key</span>
                  <code>{apiKey || DEFAULT_API_KEY}</code>
                </div>
              </div>

              <div class="secondary-actions">
                <button onclick={() => setPage("settings")}>Launch settings</button>
                <button onclick={() => copyText(serveCommand)}>
                  Copy serve command
                </button>
                <button onclick={refreshAll}>Refresh status</button>
                <button onclick={() => checkAccounts()}>Check accounts</button>
              </div>
            </article>

            <article class="module-panel capability-panel">
              <div class="panel-heading">
                <div>
                  <p class="panel-kicker">capability map</p>
                  <h2>Current API capacity</h2>
                </div>
                <div class="secondary-actions compact">
                  <button onclick={refreshUsage}>Refresh usage</button>
                  <button onclick={refreshModels}>Refresh models</button>
                </div>
              </div>

              <div class="capacity-grid">
                {#each capacityCards as card (card.id)}
                  <section class={`capacity-card ${card.tone}`}>
                    <div class="capacity-card-head">
                      <div>
                        <span>{card.label}</span>
                        <h3>{card.title}</h3>
                      </div>
                      <strong>{card.parallel}</strong>
                    </div>
                    <dl class="capacity-facts">
                      <div>
                        <dt>Route</dt>
                        <dd><code>{card.route}</code></dd>
                      </div>
                      <div>
                        <dt>Model</dt>
                        <dd>{card.model}</dd>
                      </div>
                      <div>
                        <dt>Quota</dt>
                        <dd>{card.quota}</dd>
                      </div>
                      <div>
                        <dt>Output</dt>
                        <dd>{card.output}</dd>
                      </div>
                    </dl>
                    <p>{card.note}</p>
                    <div class="capacity-account-list">
                      {#each card.accounts as account (account.name)}
                        <div class={`capacity-account ${account.tone}`}>
                          <span>{account.name}</span>
                          <small>{account.plan}</small>
                          <strong>{account.limit}x</strong>
                          <em>{account.detail}</em>
                        </div>
                      {/each}
                    </div>
                  </section>
                {/each}
              </div>

              <div class="capacity-footer">
                <span>Usage {usageCheckedAt || "not loaded"}</span>
                <span>Models {modelsCheckedAt || "not loaded"} · {modelIds.length || visibleModelIds.length} aliases</span>
                <button class="quiet-button" onclick={() => setPage("api-docs")}>
                  Open API docs
                </button>
              </div>
            </article>

            <article class="module-panel">
              <div class="panel-heading">
                <div>
                  <p class="panel-kicker">control plane</p>
                  <h2>Accounts Management</h2>
                </div>
                <button class="quiet-button" onclick={() => setPage("accounts")}>
                  Manage
                </button>
              </div>

              <div class="account-table-wrap">
                <table class="account-table">
                  <thead>
                    <tr>
                      <th>Account</th>
                      <th>Status</th>
                      <th>Mode</th>
                      <th>Last check</th>
                      <th>Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {#each mergedAccounts as row (row.account)}
                      <tr>
                        <td>
                          <strong>{row.account}</strong>
                          <small>{row.plan_type ?? row.plan_bucket ?? "unknown"}</small>
                        </td>
                        <td>
                          <span class={`health-pill ${accountHealthTone(row)}`}>
                            {accountHealthLabel(row)}
                          </span>
                        </td>
                        <td>{row.capture_exists ? "capture on" : "missing"}</td>
                        <td>{row.live ? lastHealthCheckAt || "just now" : "not checked"}</td>
                        <td>
                          <div class="row-actions">
                            <button onclick={() => checkAccounts(row.account)}>
                              Check
                            </button>
                            <button onclick={() => editAccountCapture(row.account)}>
                              Edit
                            </button>
                            <button
                              class="danger-action"
                              onclick={() => deleteAccount(row.account)}
                            >
                              Delete
                            </button>
                          </div>
                        </td>
                      </tr>
                    {:else}
                      <tr>
                        <td colspan="5">
                          No account captures loaded. Add one from Accounts.
                        </td>
                      </tr>
                    {/each}
                  </tbody>
                </table>
              </div>
            </article>

            <details class="module-panel limits-panel">
              <summary>
                <span>
                  <span class="panel-kicker">configuration</span>
                  <strong>Runtime limits</strong>
                </span>
                <em>collapsed</em>
              </summary>
              <div class="limit-list">
                {#each FEATURES as [feature, label] (feature)}
                  <div class="limit-row">
                    <span>{label}</span>
                    <code>
                      {PLANS.map(
                        (plan) =>
                          `${plan}:${concurrency?.[feature]?.plans?.[plan] ?? "-"}`,
                      ).join(" / ")}
                    </code>
                  </div>
                {/each}
              </div>
              <button class="quiet-button" onclick={() => setPage("limits")}>
                Edit throttles
              </button>
            </details>
          </div>

          <div class="observability-plane" aria-label="Observability plane">
            <article class="module-panel">
              <div class="panel-heading">
                <div>
                  <p class="panel-kicker">observability</p>
                  <h2>Live Health</h2>
                </div>
                <span class="muted-time">{lastHealthCheckAt || "not checked"}</span>
              </div>
              <div class="health-summary">
                <div>
                  <span>Known errors</span>
                  <strong>{failedAccountCount}</strong>
                </div>
                <div>
                  <span>Healthy accounts</span>
                  <strong>{checkedAccountCount ? healthyAccountCount : "-"}</strong>
                </div>
                <div>
                  <span>Route pool</span>
                  <strong>{activeAccountCount}</strong>
                </div>
              </div>
              <div class="health-list">
                {#each mergedAccounts as row (row.account)}
                  <div class="health-row">
                    <span class={`mini-dot ${accountHealthTone(row)}`}></span>
                    <strong>{row.account}</strong>
                    <small>{row.live?.error || row.live?.default_model_slug || "awaiting check"}</small>
                  </div>
                {:else}
                  <div class="health-row">
                    <span class="mini-dot warn"></span>
                    <strong>No accounts</strong>
                    <small>Paste a request capture before routing traffic.</small>
                  </div>
                {/each}
              </div>
            </article>

            <article class="module-panel">
              <div class="panel-heading">
                <div>
                  <p class="panel-kicker">observability</p>
                  <h2>API Consumer Paths</h2>
                </div>
                <button class="quiet-button" onclick={() => setPage("api-docs")}>
                  Docs
                </button>
              </div>
              <div class="endpoint-list">
                {#each routeMonitorRows as route (route.path)}
                  <div class="endpoint-row">
                    <code>{route.path}</code>
                    <span class={`route-badge ${routeStatus(route.status)}`}>
                      {route.status}
                    </span>
                    <span>{route.latency}</span>
                    <span>{route.success}</span>
                  </div>
                {/each}
              </div>
            </article>

            <article class="module-panel">
              <div class="panel-heading">
                <div>
                  <p class="panel-kicker">observability</p>
                  <h2>System Events</h2>
                </div>
                <span class="muted-time">preview</span>
              </div>
              <div class="event-list">
                {#each eventRows as event (event.title)}
                  <div class={`event-row ${event.tone}`}>
                    <span></span>
                    <div>
                      <strong>{event.title}</strong>
                      <small>{event.detail}</small>
                    </div>
                  </div>
                {/each}
              </div>
            </article>
          </div>
        </section>
      {:else if page === "accounts"}
        <section class="grid gap-4 xl:grid-cols-[minmax(0,1.3fr)_420px]">
          <article
            class="rounded-[2rem] border border-white/10 bg-slate-900/80 p-5"
          >
            <div class="flex flex-wrap items-start justify-between gap-3">
              <PanelTitle kicker="accounts" title="Accounts and captures" />
              <div class="flex gap-2">
                <button
                  class="rounded-2xl border border-white/10 bg-white/5 px-3 py-2 font-bold"
                  onclick={loadAccounts}
                >
                  Reload
                </button>
                <button
                  class="rounded-2xl bg-sky-300 px-3 py-2 font-black text-slate-950"
                  onclick={() => checkAccounts()}
                >
                  Check all
                </button>
              </div>
            </div>
            <p class="mb-4 text-sm text-slate-400">
              If an account breaks, paste a fresh conversation request from
              ChatGPT Network and save over the same account name. You can paste
              Headers/Payload text or full Copy as cURL output. That updates the
              existing local capture; it does not create a second account.
              Delete only removes local files in this project.
            </p>
            <AccountList
              accounts={mergedAccounts}
              {featureText}
              oncheck={(name) => checkAccounts(name)}
              ondelete={(name) => deleteAccount(name)}
              onedit={(name) => editAccountCapture(name)}
            />
          </article>

          <article
            class="rounded-[2rem] border border-white/10 bg-slate-900/80 p-5 xl:sticky xl:top-5 xl:self-start"
          >
            <PanelTitle kicker="capture" title="Add new account" />
            <p class="mt-2 text-sm text-slate-400">
              Supports Safari copied request summaries, Chrome DevTools
              Headers/Payload details, and Copy as cURL output. Paste one complete
              <span class="font-mono text-slate-200"
                >POST /backend-api/f/conversation</span
              >
              request from the new ChatGPT account at a time. A valid capture
              must include URL, Authorization, cookies through Cookie or -b,
              and the request JSON body. Save is refused until the required and
              recommended checks pass.
            </p>
            <div
              class="mt-4 rounded-3xl border border-sky-300/20 bg-sky-300/[0.06] p-4"
            >
              <div
                class="text-xs font-black uppercase tracking-[0.16em] text-sky-200"
              >
                New local name
              </div>
              <p class="mt-2 text-sm text-slate-300">
                Use a stable name like
                <span class="font-mono text-slate-100">pro-work</span>,
                <span class="font-mono text-slate-100">plus-01</span>, or
                <span class="font-mono text-slate-100">free-backup</span>. This
                name is used by routing, limits, and opencode.
              </p>
            </div>
            <Input
              label="Account name"
              bind:value={newCaptureAccount}
              placeholder="pro-work"
            />
            <p
              class={`account-name-help ${
                ACCOUNT_NAME_RE.test(newCaptureAccount.trim()) ? "ok" : "bad"
              }`}
            >
              {ACCOUNT_NAME_HELP} Thai names, spaces, dots, and leading dashes
              are rejected.
            </p>
            <Textarea
              label="Conversation request capture"
              bind:value={newCaptureText}
              rows={10}
            />
            <div class="mt-4 flex flex-wrap gap-2">
              <button
                class="rounded-2xl bg-sky-300 px-4 py-3 font-black text-slate-950"
                onclick={() => inspectNewCapture(false)}
                disabled={!ACCOUNT_NAME_RE.test(newCaptureAccount.trim())}
              >
                Inspect
              </button>
              <button
                class="rounded-2xl border border-white/10 bg-white/5 px-4 py-3 font-black"
                onclick={() => inspectNewCapture(true)}
                disabled={!ACCOUNT_NAME_RE.test(newCaptureAccount.trim())}
              >
                Save new account
              </button>
            </div>
            <CaptureResult result={newCaptureResult} />
          </article>
        </section>

      {:else if page === "limits"}
        <section class="grid gap-4 xl:grid-cols-[minmax(0,1.35fr)_420px]">
          <article class="rounded-3xl border border-white/10 bg-[#0b0d12] p-5">
            <div class="flex flex-wrap items-start justify-between gap-3">
              <PanelTitle kicker="runtime policy" title="Concurrency limits" />
              <div class="flex flex-wrap gap-2">
                <button
                  class="rounded-2xl bg-sky-300 px-4 py-3 font-black text-slate-950"
                  onclick={saveBridgeSettings}
                >
                  Save limits
                </button>
                <button
                  class="rounded-2xl border border-white/10 bg-white/5 px-4 py-3 font-black"
                  onclick={resetBridgeSettings}
                >
                  Reset recommended
                </button>
              </div>
            </div>
            <p class="mt-2 max-w-3xl text-sm text-slate-400">
              These are local bridge throttles. Chat controls total calls per
              account. Image and Research add stricter per-feature limits on top
              of chat, so one busy feature does not consume every route.
            </p>

            <div class="mt-5 grid gap-4">
              {#each FEATURES as [feature, label, description] (feature)}
                <section
                  class="rounded-3xl border border-white/10 bg-white/[0.03] p-4"
                >
                  <div
                    class="flex flex-wrap items-center justify-between gap-2"
                  >
                    <div>
                      <h3 class="text-lg font-black">{label}</h3>
                      <p class="text-sm text-slate-500">{description}</p>
                    </div>
                    <Badge tone={feature === "chat" ? "ok" : "warn"}>
                      {feature === "chat" ? "base limiter" : "feature limiter"}
                    </Badge>
                  </div>

                  <div class="mt-4 grid gap-3 md:grid-cols-4">
                    {#each PLANS as plan (plan)}
                      <label
                        class="block rounded-2xl border border-white/10 bg-black/30 p-3"
                      >
                        <span
                          class="block text-xs font-black uppercase tracking-[0.16em] text-slate-500"
                        >
                          {plan}
                        </span>
                        <input
                          class="mt-2 w-full rounded-xl border border-white/10 bg-[#050609] px-3 py-2 font-mono text-lg font-black outline-none focus:border-sky-300/60"
                          type="number"
                          min={feature === "chat" ? "1" : "0"}
                          max="32"
                          value={concurrency[feature].plans[plan]}
                          oninput={(event) =>
                            setPlanLimit(
                              feature,
                              plan,
                              (event.currentTarget as HTMLInputElement).value,
                            )}
                        />
                      </label>
                    {/each}
                  </div>

                  <div
                    class="mt-4 rounded-2xl border border-white/10 bg-black/25 p-3"
                  >
                    <div
                      class="text-xs font-black uppercase tracking-[0.16em] text-slate-500"
                    >
                      Account overrides
                    </div>
                    {#if accounts.length}
                      <div class="mt-3 grid gap-2 md:grid-cols-2">
                        {#each accounts as account (account.account)}
                          <div
                            class="grid grid-cols-[minmax(0,1fr)_5rem_auto] items-center gap-2 rounded-xl bg-white/[0.03] p-2"
                          >
                            <div class="min-w-0">
                              <div class="truncate text-sm font-bold">
                                {account.account}
                              </div>
                              <div class="text-xs text-slate-500">
                                plan {account.plan_type ||
                                  account.plan_bucket ||
                                  planForAccount(account.account)}
                              </div>
                            </div>
                            <input
                              class="rounded-xl border border-white/10 bg-[#050609] px-2 py-2 text-center font-mono font-black outline-none focus:border-sky-300/60"
                              type="number"
                              min={feature === "chat" ? "1" : "0"}
                              max="32"
                              value={concurrency[feature].accounts[
                                account.account
                              ] ??
                                concurrency[feature].plans[
                                  account.plan_type ||
                                    account.plan_bucket ||
                                    planForAccount(account.account)
                                ] ??
                                1}
                              oninput={(event) =>
                                setAccountLimit(
                                  feature,
                                  account.account,
                                  (event.currentTarget as HTMLInputElement)
                                    .value,
                                )}
                            />
                            <button
                              class="rounded-xl border border-white/10 px-2 py-2 text-xs font-bold text-slate-400 hover:text-white"
                              onclick={() =>
                                clearAccountLimit(feature, account.account)}
                            >
                              inherit
                            </button>
                          </div>
                        {/each}
                      </div>
                    {:else}
                      <p class="mt-3 text-sm text-slate-500">
                        Add account captures first to create account-specific
                        overrides.
                      </p>
                    {/if}
                  </div>
                </section>
              {/each}
            </div>
          </article>

          <aside class="grid gap-4 self-start xl:sticky xl:top-5">
            <article
              class="rounded-3xl border border-white/10 bg-[#0b0d12] p-5"
            >
              <PanelTitle kicker="recommended" title="Safe defaults" />
              <div class="mt-4 grid gap-3">
                <div
                  class="rounded-2xl border border-white/10 bg-white/[0.03] p-3"
                >
                  <div class="font-black">Chat</div>
                  <div class="mt-1 text-sm text-slate-400">
                    free 1 · go 2 · plus 3 · pro 4
                  </div>
                </div>
                <div
                  class="rounded-2xl border border-white/10 bg-white/[0.03] p-3"
                >
                  <div class="font-black">Upload</div>
                  <div class="mt-1 text-sm text-slate-400">
                    free/go/plus/pro 1. OCR, describe, chat-with-image, and
                    source-image edit/composite uploads share this bucket.
                  </div>
                </div>
                <div
                  class="rounded-2xl border border-white/10 bg-white/[0.03] p-3"
                >
                  <div class="font-black">Image</div>
                  <div class="mt-1 text-sm text-slate-400">
                    pro 3 recommended. Higher can work, but burst limits are
                    hidden.
                  </div>
                </div>
                <div
                  class="rounded-2xl border border-white/10 bg-white/[0.03] p-3"
                >
                  <div class="font-black">Research</div>
                  <div class="mt-1 text-sm text-slate-400">
                    plus/pro 2 recommended. Free/Go default to 0 because Deep
                    Research is not reliable there.
                  </div>
                </div>
              </div>
            </article>

            <article
              class="rounded-3xl border border-amber-300/20 bg-amber-300/[0.06] p-5"
            >
              <PanelTitle kicker="rate limits" title="Hidden cooldowns" />
              <p class="text-sm text-amber-100/80">
                Even when Pro shows a large daily image quota, generating too
                fast can still trigger a hidden 5-10 minute cooldown. These
                settings reduce burst pressure; they do not remove ChatGPT's
                server-side limits.
              </p>
            </article>

            <article
              class="rounded-3xl border border-white/10 bg-[#0b0d12] p-5"
            >
              <PanelTitle kicker="runtime" title="Current applied values" />
              <MetricGrid
                items={[
                  ["Chat", JSON.stringify(routing.account_concurrency ?? {})],
                  [
                    "Upload",
                    JSON.stringify(routing.feature_concurrency?.upload ?? {}),
                  ],
                  [
                    "Image",
                    JSON.stringify(routing.feature_concurrency?.image ?? {}),
                  ],
                  [
                    "Research",
                    JSON.stringify(routing.feature_concurrency?.research ?? {}),
                  ],
                ]}
              />
              <CodeBlock
                title="Settings save result"
                code={settingsResult || "No settings saved in this session."}
              />
            </article>
          </aside>
        </section>
      {:else if page === "test-lab"}
        <section class="grid gap-4 xl:grid-cols-2">
          <article
            class="rounded-[2rem] border border-white/10 bg-slate-900/80 p-5"
          >
            <PanelTitle kicker="chat" title="Single message test" />
            <Input label="Model" bind:value={chatModel} />
            <Textarea label="Message" bind:value={chatPrompt} rows={5} />
            <button
              class="mt-4 rounded-2xl bg-sky-300 px-4 py-3 font-black text-slate-950"
              onclick={runChat}
            >
              Run chat
            </button>
            <CodeBlock title="curl" code={chatCurl} />
            <pre
              class="mt-4 min-h-40 max-w-full overflow-auto whitespace-pre-wrap break-words rounded-2xl border border-white/10 bg-slate-950 p-4 text-sm text-slate-300">{chatResult}</pre>
          </article>

          <article
            class="rounded-[2rem] border border-white/10 bg-slate-900/80 p-5"
          >
            <PanelTitle kicker="context" title="Carry context test" />
            <p class="text-sm text-slate-400">
              This sends prior messages in one request. Your app owns the
              conversation memory and passes the messages it wants remembered.
            </p>
            <Textarea
              label="System prompt"
              bind:value={contextSystem}
              rows={4}
            />
            <Textarea
              label="Earlier user message"
              bind:value={contextSetup}
              rows={4}
            />
            <Textarea
              label="Earlier assistant reply"
              bind:value={contextAssistant}
              rows={3}
            />
            <Textarea
              label="Current user message"
              bind:value={contextQuestion}
              rows={3}
            />
            <button
              class="mt-4 rounded-2xl bg-sky-300 px-4 py-3 font-black text-slate-950"
              onclick={runContextChat}
            >
              Run context test
            </button>
            <CodeBlock title="curl" code={contextCurl} />
            <pre
              class="mt-4 min-h-40 max-w-full overflow-auto whitespace-pre-wrap break-words rounded-2xl border border-white/10 bg-slate-950 p-4 text-sm text-slate-300">{contextResult}</pre>
          </article>

          <article
            class="rounded-[2rem] border border-white/10 bg-slate-900/80 p-5"
          >
            <PanelTitle kicker="image" title="Image route test" />
            <Input label="Model" bind:value={imageModel} />
            <Textarea label="Prompt" bind:value={imagePrompt} rows={5} />
            <button
              class="mt-4 rounded-2xl bg-sky-300 px-4 py-3 font-black text-slate-950"
              onclick={runImage}
            >
              Generate image
            </button>
            <CodeBlock title="curl" code={imageCurl} />
            <ImageResult result={imageResult} {downloadUrl} />
          </article>

          <article
            class="rounded-[2rem] border border-white/10 bg-slate-900/80 p-5"
          >
            <PanelTitle kicker="research" title="Deep Research test" />
            <p class="text-sm text-slate-400">
              Uses normal ChatGPT chat mode internally. Temporary chat is not
              used for research, and reports are saved to the research output
              directory.
            </p>
            <Textarea
              label="Research prompt"
              bind:value={researchPrompt}
              rows={5}
            />
            <button
              class="mt-4 rounded-2xl border border-amber-300/40 bg-amber-300/10 px-4 py-3 font-black text-amber-100"
              onclick={runResearch}
            >
              Start research
            </button>
            <pre
              class="mt-4 min-h-40 max-w-full overflow-auto whitespace-pre-wrap break-words rounded-2xl border border-white/10 bg-slate-950 p-4 text-sm text-slate-300">{researchResult}</pre>
          </article>
        </section>
      {:else if page === "api-docs"}
        <section class="grid gap-4">
          <article
            class="rounded-[2rem] border border-white/10 bg-[#0b0d12] p-5"
          >
            <PanelTitle
              kicker="docs"
              title="How this bridge is meant to be used"
            />
            <div class="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
              {#each [["Base URL", baseUrl, "Every route below is relative to this /v1 URL."], ["Auth", apiKey ? "Bearer key required" : "No auth", "Default dev key is local-dev-key."], ["Not a full clone", "Bridge-style API", "Close to chat completions, with ChatGPT Web specific behavior."], ["Files", "Download route", "Images/reports get local paths and HTTP download links."]] as item (item[0])}
                <div
                  class="rounded-2xl border border-white/10 bg-white/[0.03] p-4"
                >
                  <div
                    class="text-xs font-black uppercase tracking-[0.16em] text-slate-500"
                  >
                    {item[0]}
                  </div>
                  <div class="mt-2 break-all font-black text-slate-100">
                    {item[1]}
                  </div>
                  <p class="mt-2 text-xs text-slate-400">{item[2]}</p>
                </div>
              {/each}
            </div>
            <div class="mt-4 grid gap-3 lg:grid-cols-3">
              <div class="rounded-2xl border border-white/10 bg-black/20 p-4">
                <div class="font-black text-slate-100">Client apps</div>
                <p class="mt-2 text-sm text-slate-400">
                  Use `/chat/completions` for normal app chat, game narration,
                  context replay, and opencode tool bridge calls.
                </p>
              </div>
              <div class="rounded-2xl border border-white/10 bg-black/20 p-4">
                <div class="font-black text-slate-100">Long jobs</div>
                <p class="mt-2 text-sm text-slate-400">
                  Image and Deep Research can take longer. Clients should keep
                  the operation id and cancel it when the tab closes or Ctrl-C
                  happens.
                </p>
              </div>
              <div class="rounded-2xl border border-white/10 bg-black/20 p-4">
                <div class="font-black text-slate-100">Docker/headless</div>
                <p class="mt-2 text-sm text-slate-400">
                  Everything important has a CLI path. The web console is
                  convenience, not the only control surface.
                </p>
              </div>
            </div>
          </article>

          <article
            class="rounded-[2rem] border border-cyan-300/15 bg-[#071018]/95 p-5"
          >
            <div class="flex flex-wrap items-start justify-between gap-3">
              <PanelTitle
                kicker="model catalog"
                title="Available models and feature paths"
              />
              <div class="flex flex-wrap gap-2">
                <button
                  class="rounded-xl border border-cyan-300/30 bg-cyan-300/10 px-4 py-2 text-sm font-black text-cyan-50"
                  onclick={refreshModels}
                >
                  Refresh models
                </button>
                <button
                  class="rounded-xl border border-white/10 bg-white/5 px-4 py-2 text-sm font-black text-slate-100"
                  onclick={() => copyText(visibleModelIds.join("\n"))}
                >
                  Copy model ids
                </button>
              </div>
            </div>
            <p class="mt-2 max-w-4xl text-sm leading-relaxed text-slate-400">
              `/v1/models` is the source of truth for the current route. Free
              and Go accounts should use `auto`. Paid accounts may expose
              explicit GPT-5.5, thinking, and pro aliases. Image and Research
              are bridge features, so they use their own aliases and routes.
            </p>
            <div class="mt-5 grid gap-3 xl:grid-cols-2">
              {#each modelGroups as group (group.id)}
                <section
                  class="rounded-2xl border border-white/10 bg-black/25 p-4"
                >
                  <div class="flex items-start justify-between gap-3">
                    <div>
                      <h3 class="text-lg font-black text-white">
                        {group.title}
                      </h3>
                      <p class="mt-1 text-sm text-slate-400">{group.note}</p>
                    </div>
                    <Badge>{group.ids.length} ids</Badge>
                  </div>
                  <div class="mt-4 grid gap-2">
                    {#each group.ids as id (id)}
                      <div class="model-id-row">
                        <code>{id}</code>
                        <span>{modelHelp(id)}</span>
                      </div>
                    {/each}
                  </div>
                </section>
              {/each}
            </div>
            <div class="mt-4 grid gap-3 lg:grid-cols-4">
              <div class="rounded-2xl border border-white/10 bg-black/20 p-4">
                <div class="font-black text-slate-100">Chat</div>
                <p class="mt-2 text-sm text-slate-400">
                  Route: `POST /v1/chat/completions`. Use `stream=true` for
                  fast UI. Use `@optimized` or `@opencode` only for tool-agent
                  clients.
                </p>
              </div>
              <div class="rounded-2xl border border-white/10 bg-black/20 p-4">
                <div class="font-black text-slate-100">Images</div>
                <p class="mt-2 text-sm text-slate-400">
                  Route: `POST /v1/images/generations`. Use `gpt-image-1`.
                  Completed files return `download_url` and local `path`.
                </p>
              </div>
              <div class="rounded-2xl border border-white/10 bg-black/20 p-4">
                <div class="font-black text-slate-100">Vision + Edit</div>
                <p class="mt-2 text-sm text-slate-400">
                  Routes: `POST /v1/chatgpt/vision` and `POST
                  /v1/images/edits`. Vision can return plain OCR text or
                  prompt-shaped JSON/bbox; edits return one image. Source
                  images use file_upload usage when ChatGPT reports it.
                </p>
              </div>
              <div class="rounded-2xl border border-white/10 bg-black/20 p-4">
                <div class="font-black text-slate-100">Deep Research</div>
                <p class="mt-2 text-sm text-slate-400">
                  Route: `POST /v1/chat/completions`. Use
                  `chatgpt-deep-research`; result is a markdown artifact with a
                  download URL.
                </p>
              </div>
            </div>
          </article>

          <ApiFieldGuide guides={API_FIELD_GUIDES} />
          <ResponseFieldGuide fields={RESPONSE_FIELD_GUIDES} />

          <article
            class="rounded-2xl border border-cyan-300/20 bg-[#071018]/90 p-4 shadow-[0_24px_80px_rgba(0,0,0,0.28)] sm:p-5"
          >
            <div class="flex flex-wrap items-start justify-between gap-3">
              <PanelTitle
                kicker="route contracts"
                title="What every route returns"
              />
              <button
                class="rounded-xl border border-cyan-300/30 bg-cyan-300/10 px-4 py-2 text-sm font-black text-cyan-50"
                onclick={() =>
                  copyText(
                    routeResponseDocs
                      .map(
                        (doc) =>
                          `${doc.route}\nread: ${doc.read}\nfiles: ${doc.files}\noperation: ${doc.operation}\nresponse:\n${typeof doc.response === "string" ? doc.response : JSON.stringify(doc.response, null, 2)}`,
                      )
                      .join("\n\n---\n\n"),
                  )}
              >
                Copy response map
              </button>
            </div>
            <p class="mt-2 max-w-4xl text-sm leading-relaxed text-slate-400">
              Use this section when wiring a real app. It tells the client which
              field to read, whether a file is created, and how cancel/download
              should work. Files for images and Deep Research are always exposed
              through the same download route.
            </p>
            <div class="mt-5 grid gap-3 xl:grid-cols-2">
              {#each routeResponseDocs as doc (doc.route)}
                <article
                  class="overflow-hidden rounded-2xl border border-white/10 bg-black/25"
                >
                  <div
                    class="grid gap-3 border-b border-white/10 bg-white/[0.035] p-4 md:grid-cols-[minmax(0,1fr)_auto]"
                  >
                    <div class="min-w-0">
                      <div
                        class="break-all font-mono text-xs font-black text-cyan-100"
                      >
                        {doc.route}
                      </div>
                      <div class="mt-2 text-lg font-black text-white">
                        {doc.kind}
                      </div>
                    </div>
                    <Badge>{doc.kind}</Badge>
                  </div>
                  <div class="grid gap-3 p-4 text-sm">
                    <div class="grid gap-3 md:grid-cols-3">
                      <div>
                        <div class="text-[11px] font-black uppercase tracking-[0.16em] text-slate-500">
                          client reads
                        </div>
                        <p class="mt-1 break-words text-slate-200">
                          {doc.read}
                        </p>
                      </div>
                      <div>
                        <div class="text-[11px] font-black uppercase tracking-[0.16em] text-slate-500">
                          files
                        </div>
                        <p class="mt-1 break-words text-slate-200">
                          {doc.files}
                        </p>
                      </div>
                      <div>
                        <div class="text-[11px] font-black uppercase tracking-[0.16em] text-slate-500">
                          cancel / job
                        </div>
                        <p class="mt-1 break-words text-slate-200">
                          {doc.operation}
                        </p>
                      </div>
                    </div>
                    <CodeBlock
                      title="response shape"
                      code={typeof doc.response === "string"
                        ? doc.response
                        : JSON.stringify(doc.response, null, 2)}
                    />
                  </div>
                </article>
              {/each}
            </div>
          </article>

          <article
            class="rounded-[2rem] border border-white/10 bg-slate-900/80 p-4 sm:p-5"
          >
            <PanelTitle kicker="examples" title="Response examples" />
            <p class="mt-2 max-w-3xl text-sm text-slate-400">
              These are representative payloads a client should handle. The
              exact text and ids change per request, but the fields and download
              rules are stable bridge behavior.
            </p>
            <div class="mt-4 grid gap-4 xl:grid-cols-2">
              {#each responseExamples as example (example.title)}
                <article
                  class="min-w-0 rounded-3xl border border-white/10 bg-white/[0.03] p-4"
                >
                  <h3 class="text-xl font-black">{example.title}</h3>
                  <p class="mt-2 text-sm text-slate-400">{example.body}</p>
                  <CodeBlock title="JSON" code={example.code} />
                </article>
              {/each}
            </div>
          </article>

          <article
            class="rounded-[2rem] border border-amber-300/20 bg-amber-300/[0.06] p-4 sm:p-5"
          >
            <PanelTitle kicker="downloads" title="Files, images, and reports" />
            <p class="mt-2 max-w-3xl text-sm text-amber-100/80">
              Images and Deep Research reports are local files plus HTTP
              downloads. Same-machine apps may use filesystem paths. Browsers,
              phones, Docker clients, and LAN machines must use the returned
              download URL.
            </p>
            <div class="mt-4 grid gap-3 lg:grid-cols-2">
              {#each downloadGuides as guide (guide.title)}
                <div
                  class="min-w-0 rounded-2xl border border-amber-200/15 bg-black/25 p-4"
                >
                  <div class="font-black text-amber-50">{guide.title}</div>
                  <p class="mt-2 text-sm text-amber-100/75">{guide.body}</p>
                  <code
                    class="mt-3 block break-all rounded-xl bg-black/30 p-3 text-xs text-amber-50"
                    >{guide.example}</code
                  >
                </div>
              {/each}
            </div>
          </article>

          <article
            class="rounded-[2rem] border border-white/10 bg-slate-900/80 p-5"
          >
            <div class="flex flex-wrap items-start justify-between gap-3">
              <PanelTitle kicker="api reference" title="Copy/paste routes" />
              <button
                class="rounded-2xl bg-sky-300 px-4 py-3 font-black text-slate-950"
                onclick={() =>
                  copyText(
                    apiDocs
                      .map((doc) => `# ${doc.title}\n${doc.code}`)
                      .join("\n\n"),
                  )}
              >
                Copy all curl
              </button>
            </div>
            <p class="mb-4 text-sm text-slate-400">
              These examples are generated from your current Settings. They
              cover consumer API routes and admin management routes. The curl
              generator keeps every continued line escaped so copied commands
              work in zsh.
            </p>
            <div class="grid gap-4 xl:grid-cols-2">
              {#each apiDocs as doc (doc.title)}
                <article
                  class="rounded-3xl border border-white/10 bg-white/[0.03] p-4"
                >
                  <div
                    class="text-xs font-black uppercase tracking-[0.16em] text-sky-200"
                  >
                    {doc.path}
                  </div>
                  <h3 class="mt-2 text-xl font-black">{doc.title}</h3>
                  <p class="mt-2 text-sm text-slate-400">{doc.note}</p>
                  <CodeBlock title="curl" code={doc.code} />
                </article>
              {/each}
            </div>
          </article>

          <article
            class="rounded-[2rem] border border-white/10 bg-slate-900/80 p-5"
          >
            <PanelTitle kicker="cli" title="Headless and Docker commands" />
            <p class="mb-4 text-sm text-slate-400">
              Use these commands when the server runs in Docker, SSH, tmux,
              screen, or CI. They call the same admin routes as this console.
            </p>
            <div class="grid gap-4 xl:grid-cols-2">
              {#each cliDocs as doc (doc.title)}
                <article
                  class="rounded-3xl border border-white/10 bg-white/[0.03] p-4"
                >
                  <h3 class="text-xl font-black">{doc.title}</h3>
                  <p class="mt-2 text-sm text-slate-400">{doc.note}</p>
                  <CodeBlock title="command" code={doc.code} />
                </article>
              {/each}
            </div>
          </article>

          <article
            class="rounded-[2rem] border border-white/10 bg-slate-900/80 p-5"
          >
            <PanelTitle
              kicker="troubleshooting"
              title="What to check when it breaks"
            />
            <div class="mt-4 grid gap-3 md:grid-cols-2">
              {#each [["401 unauthorized", "Bearer key does not match the running API server. Check the key in Launch or use the default local-dev-key."], ["missing=url,authorization,cookie", "The capture paste is incomplete. For cURL, paste the whole command including the ChatGPT URL, Authorization, Cookie or -b cookie jar text, and --data-raw JSON."], ["request body must be JSON", "The copied local curl command lost a trailing backslash or Content-Type header. Copy the generated block again."], ["Chat hangs before any text", "Refresh token/prepare may be slow. The bridge now fails token refresh within 30 seconds instead of hanging for Deep Research timeout."], ["Research did not cancel immediately", "Cancel is best effort. The bridge reads the Deep Research widget session from WSS, then sends the MCP stop call when the session id is known."], ["Account expired", "Open Accounts, click Update capture on that account, paste a fresh POST /backend-api/f/conversation request, then Save account."], ["Image is listed but file missing", "Library now hides and prunes stale DB records. A visible item must have exists=true and a real download link."], ["LAN client cannot download files", "Set Public base URL to the machine's LAN address, not 127.0.0.1, before starting the API server."]] as row (row[0])}
                <div class="rounded-2xl border border-white/10 bg-black/20 p-4">
                  <div class="font-black text-slate-100">{row[0]}</div>
                  <p class="mt-2 text-sm text-slate-400">{row[1]}</p>
                </div>
              {/each}
            </div>
          </article>
        </section>
      {:else if page === "storage"}
        <section
          class="rounded-[2rem] border border-white/10 bg-slate-900/80 p-5"
        >
          <div class="flex flex-wrap items-start justify-between gap-3">
            <PanelTitle kicker="storage" title="Artifacts and downloads" />
            <button
              class="rounded-2xl border border-white/10 bg-white/5 px-4 py-3 font-black"
              onclick={loadArtifacts}
            >
              Reload
            </button>
          </div>
          <p class="mb-4 text-sm text-slate-400">
            Files are served through the bridge download route, so LAN clients
            can open them without direct filesystem access.
          </p>
          <div class="grid gap-4 md:grid-cols-2 2xl:grid-cols-3">
            {#each artifacts as artifact (artifact.file_id)}
              <article
                class="overflow-hidden rounded-3xl border border-white/10 bg-white/[0.03]"
              >
                {#if isImageArtifact(artifact) && artifact.exists}
                  <a
                    class="block bg-slate-950"
                    href={downloadUrl(artifact.download_url)}
                    target="_blank"
                    rel="noreferrer"
                  >
                    <img
                      class="aspect-[16/10] w-full object-cover transition duration-300 hover:scale-[1.02]"
                      src={downloadUrl(artifact.download_url)}
                      alt={artifact.filename}
                    />
                  </a>
                {:else}
                  <div
                    class="grid aspect-[16/10] place-items-center bg-slate-950 p-6 text-center text-sm text-slate-400"
                  >
                    <div>
                      <div class="text-4xl font-black text-slate-700">MD</div>
                      <div class="mt-2">Report or non-image artifact</div>
                    </div>
                  </div>
                {/if}
                <div class="min-w-0 p-4">
                  <h3 class="break-all text-lg font-black">
                    {artifact.filename}
                  </h3>
                  <div class="mt-1 text-xs text-slate-500">
                    {artifact.kind} · {artifact.account || "unknown"} · {formatBytes(
                      artifact.bytes,
                    )}
                  </div>
                  <div class="mt-1 break-all font-mono text-xs text-slate-400">
                    {artifact.path}
                  </div>
                  <div class="mt-3 flex flex-wrap gap-2">
                    <Badge tone={artifact.exists ? "ok" : "bad"}
                      >{artifact.exists ? "file exists" : "file missing"}</Badge
                    >
                    <Badge>{artifact.content_type || "unknown type"}</Badge>
                    <a
                      class="rounded-full border border-sky-300/40 px-3 py-1 text-xs font-bold text-sky-100"
                      href={downloadUrl(artifact.download_url)}
                      target="_blank"
                      rel="noreferrer"
                    >
                      open download
                    </a>
                  </div>
                  {#if artifact.prompt}
                    <details
                      class="mt-3 rounded-2xl border border-white/10 bg-black/25"
                    >
                      <summary
                        class="cursor-pointer px-3 py-2 text-xs font-bold text-slate-300"
                      >
                        Prompt / source
                      </summary>
                      <p
                        class="max-h-40 overflow-auto px-3 pb-3 text-xs text-slate-400"
                      >
                        {artifact.prompt}
                      </p>
                    </details>
                  {/if}
                  <div class="mt-4 flex flex-wrap items-start gap-2">
                    <button
                      class="rounded-2xl border border-white/10 bg-white/5 px-3 py-2 text-sm font-bold"
                      onclick={() => deleteArtifact(artifact, false)}
                    >
                      Delete record
                    </button>
                    <button
                      class="rounded-2xl border border-rose-300/40 bg-rose-300/10 px-3 py-2 text-sm font-bold text-rose-100"
                      onclick={() => deleteArtifact(artifact, true)}
                    >
                      Delete file
                    </button>
                  </div>
                </div>
              </article>
            {:else}
              <div
                class="rounded-3xl border border-white/10 bg-white/[0.03] p-8 text-slate-400"
              >
                No artifacts recorded yet.
              </div>
            {/each}
          </div>
        </section>
      {:else if page === "opencode"}
        <section
          class="grid gap-4 xl:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]"
        >
          <article class="rounded-3xl border border-white/10 bg-[#0b0d12] p-5">
            <PanelTitle kicker="opencode" title="Consumer config only" />
            <p class="text-sm text-slate-400">
              This section only writes or removes opencode provider config. It
              does not configure ChatGPT accounts, quotas, output folders, or
              server launch settings.
            </p>
            <Input
              label="Local config path"
              bind:value={opencodePath}
              placeholder="~/.config/opencode/opencode.json"
            />
            <Input label="Model" bind:value={opencodeModel} />
            <div class="mt-4 flex flex-wrap gap-2">
              <button
                class="rounded-2xl bg-sky-300 px-4 py-3 font-black text-slate-950"
                onclick={injectOpencode}
              >
                Inject on this machine
              </button>
              <button
                class="rounded-2xl border border-rose-300/40 bg-rose-300/10 px-4 py-3 font-black text-rose-100"
                onclick={ejectOpencode}
              >
                Eject
              </button>
            </div>
            <div
              class="mt-5 rounded-3xl border border-amber-300/20 bg-amber-300/10 p-4 text-sm text-amber-50"
            >
              LAN note: start the API with host `0.0.0.0`, set public base URL
              to `http://YOUR-LAN-IP:8000/v1`, then use that base URL in the
              client opencode config.
            </div>
            <CodeBlock
              title="Quick terminal setup"
              code={opencodeQuickCommand}
            />
          </article>
          <article class="rounded-3xl border border-white/10 bg-[#0b0d12] p-5">
            <PanelTitle kicker="lan" title="LAN client snippets" />
            <div
              class="mb-4 rounded-2xl border border-sky-300/20 bg-sky-300/10 p-4 text-sm text-sky-50"
            >
              opencode executes tools on the client side. The bridge only
              returns tool calls, so use the `@opencode` model suffix for bigger
              coding tasks and `@optimized` for small, cheap edits.
            </div>
            <CodeBlock title="Provider JSON" code={opencodeLanJson} />
            <CodeBlock title="Inject curl" code={opencodeLanCurl} />
          </article>
        </section>
      {:else if page === "settings"}
        <section
          class="grid gap-4 xl:grid-cols-[minmax(0,0.95fr)_minmax(0,1.05fr)]"
        >
          <article class="rounded-3xl border border-white/10 bg-[#0b0d12] p-5">
            <PanelTitle
              kicker="console"
              title="Bridge target this console calls"
            />
            <p class="text-sm text-slate-400">
              This only tells this console which bridge API to call. It does not
              start the API server or change ChatGPT accounts.
            </p>
            <Input label="Bridge API base URL" bind:value={baseUrl} />
            <Input label="Bearer key" bind:value={apiKey} />
            <div class="mt-4 flex flex-wrap gap-2">
              <button
                class="rounded-2xl bg-sky-300 px-4 py-3 font-black text-slate-950"
                onclick={saveConnection}
              >
                Save target
              </button>
              <button
                class="rounded-2xl border border-white/10 bg-white/5 px-4 py-3 font-black"
                onclick={resetDefaultConnection}
              >
                Reset defaults
              </button>
              <button
                class="rounded-2xl border border-white/10 bg-white/5 px-4 py-3 font-black"
                onclick={refreshAll}
              >
                Test target
              </button>
            </div>
          </article>

          <article class="rounded-3xl border border-white/10 bg-[#0b0d12] p-5">
            <PanelTitle kicker="launch" title="Server launch presets" />
            <p class="text-sm text-slate-400">
              Pick a preset to fill the command fields. The API server and this
              console are separate processes; run both terminals when developing
              locally.
            </p>
            <div class="mt-4 grid gap-3">
              {#each presetCards() as preset (preset.id)}
                <button
                  class={`rounded-2xl border p-4 text-left transition ${
                    selectedPreset === preset.id
                      ? "border-sky-300/40 bg-sky-300/[0.08]"
                      : "border-white/10 bg-white/[0.03] hover:bg-white/[0.06]"
                  }`}
                  onclick={() => applyServerPreset(preset.id)}
                >
                  <div class="flex items-center justify-between gap-3">
                    <div class="font-black">{preset.title}</div>
                    <Badge
                      tone={selectedPreset === preset.id ? "ok" : "neutral"}
                    >
                      {selectedPreset === preset.id ? "selected" : "apply"}
                    </Badge>
                  </div>
                  <p class="mt-2 text-sm text-slate-400">{preset.body}</p>
                  <div class="mt-2 font-mono text-xs text-slate-500">
                    {preset.values}
                  </div>
                </button>
              {/each}
            </div>

            <div
              class="mt-4 rounded-2xl border border-white/10 bg-black/25 p-4"
            >
              <div
                class="text-xs font-black uppercase tracking-[0.16em] text-slate-500"
              >
                Current launch shape
              </div>
              <div
                class="mt-3 grid gap-2 text-sm text-slate-300 sm:grid-cols-2"
              >
                <div>host <code>{serverHost}</code></div>
                <div>port <code>{serverPort}</code></div>
                <div>accounts <code>{serverAccounts || "auto-discover"}</code></div>
                <div>strategy <code>{serverStrategy}</code></div>
              </div>
            </div>

            <div
              class="mt-4 rounded-3xl border border-white/10 bg-black/20 p-4"
            >
              <div class="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <div class="text-sm font-black text-slate-100">
                    Settings guide
                  </div>
                  <p class="mt-1 text-xs text-slate-500">
                    These controls produce the serve command below and should be
                    the same values you use for Docker or LAN clients.
                  </p>
                </div>
                <Badge>launch fields</Badge>
              </div>
              <div class="mt-3 grid gap-2 md:grid-cols-2">
                {#each LAUNCH_FIELD_GUIDES as [name, description] (name)}
                  <div
                    class="rounded-2xl border border-white/10 bg-white/[0.03] p-3"
                  >
                    <div class="text-sm font-black text-slate-100">{name}</div>
                    <p class="mt-1 text-xs leading-relaxed text-slate-400">
                      {description}
                    </p>
                  </div>
                {/each}
              </div>
            </div>

            <div class="mt-5 grid gap-4">
              <div class="grid gap-3 lg:grid-cols-2">
                <label class="block">
                  <span class="text-sm font-bold text-slate-300">Host</span>
                  <select
                    class="mt-2 w-full rounded-2xl border border-white/10 bg-slate-950 px-3 py-3 outline-none focus:border-sky-300/60"
                    bind:value={serverHost}
                  >
                    {#each HOST_OPTIONS as [value, label, description] (value)}
                      <option {value}>{label} - {value} - {description}</option>
                    {/each}
                  </select>
                </label>
                <label class="block">
                  <span class="text-sm font-bold text-slate-300">Port</span>
                  <select
                    class="mt-2 w-full rounded-2xl border border-white/10 bg-slate-950 px-3 py-3 outline-none focus:border-sky-300/60"
                    bind:value={serverPort}
                  >
                    {#each PORT_OPTIONS as [value, label] (value)}
                      <option {value}>{label} - {value}</option>
                    {/each}
                  </select>
                </label>
              </div>

              <div class="rounded-3xl border border-white/10 bg-black/20 p-4">
                <div class="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <div class="text-sm font-black text-slate-100">
                      Accounts used by this API server
                    </div>
                    <p class="mt-1 text-xs text-slate-500">
                      Free/Go should stay on auto model. Put stronger accounts
                      first when you want faster tests.
                    </p>
                  </div>
                  <div class="flex gap-2">
                    <button
                      class="rounded-xl border border-white/10 bg-white/5 px-3 py-2 text-xs font-black"
                      onclick={() => setServerAccounts(accountNames)}
                    >
                      All
                    </button>
                    <button
                      class="rounded-xl border border-white/10 bg-white/5 px-3 py-2 text-xs font-black"
                      onclick={() => setServerAccounts(["pro"])}
                    >
                      Pro only
                    </button>
                  </div>
                </div>
                <div class="mt-3 grid gap-2 sm:grid-cols-2">
                  {#each accountNames as account (account)}
                    <label
                      class={`flex cursor-pointer items-center justify-between rounded-2xl border px-3 py-3 text-sm ${
                        selectedServerAccounts.includes(account)
                          ? "border-sky-300/40 bg-sky-300/[0.08]"
                          : "border-white/10 bg-white/[0.03]"
                      }`}
                    >
                      <span class="font-black">{account}</span>
                      <input
                        class="h-4 w-4"
                        type="checkbox"
                        checked={selectedServerAccounts.includes(account)}
                        onchange={() => toggleServerAccount(account)}
                      />
                    </label>
                  {/each}
                </div>
              </div>

              <div class="grid gap-3 lg:grid-cols-2">
                <label class="block">
                  <span class="text-sm font-bold text-slate-300"
                    >Primary account</span
                  >
                  <select
                    class="mt-2 w-full rounded-2xl border border-white/10 bg-slate-950 px-3 py-3 outline-none focus:border-sky-300/60"
                    bind:value={serverAccount}
                  >
                    {#each selectedServerAccounts as account (account)}
                      <option value={account}>{account}</option>
                    {/each}
                  </select>
                </label>
                <label class="block">
                  <span class="text-sm font-bold text-slate-300">Strategy</span>
                  <select
                    class="mt-2 w-full rounded-2xl border border-white/10 bg-slate-950 px-3 py-3 outline-none focus:border-sky-300/60"
                    bind:value={serverStrategy}
                  >
                    {#each STRATEGY_OPTIONS as [value, label, description] (value)}
                      <option {value}>{label} - {description}</option>
                    {/each}
                  </select>
                </label>
                <label class="block">
                  <span class="text-sm font-bold text-slate-300"
                    >Agent prompt mode</span
                  >
                  <select
                    class="mt-2 w-full rounded-2xl border border-white/10 bg-slate-950 px-3 py-3 outline-none focus:border-sky-300/60"
                    bind:value={agentMode}
                  >
                    {#each AGENT_MODE_OPTIONS as [value, label, description] (value)}
                      <option {value}>{label} - {description}</option>
                    {/each}
                  </select>
                </label>
                <label class="block">
                  <span class="text-sm font-bold text-slate-300"
                    >Model fallback</span
                  >
                  <select
                    class="mt-2 w-full rounded-2xl border border-white/10 bg-slate-950 px-3 py-3 outline-none focus:border-sky-300/60"
                    bind:value={modelFallback}
                  >
                    {#each FALLBACK_OPTIONS as [value, label, description] (value)}
                      <option {value}>{label} - {description}</option>
                    {/each}
                  </select>
                </label>
                <label class="block">
                  <span class="text-sm font-bold text-slate-300"
                    >Web timeout</span
                  >
                  <select
                    class="mt-2 w-full rounded-2xl border border-white/10 bg-slate-950 px-3 py-3 outline-none focus:border-sky-300/60"
                    bind:value={webTimeout}
                  >
                    {#each TIMEOUT_OPTIONS as [value, label] (value)}
                      <option {value}>{label} - {value}s</option>
                    {/each}
                  </select>
                </label>
                <label
                  class="flex items-center justify-between rounded-2xl border border-white/10 bg-white/[0.03] px-4 py-3 text-sm font-bold text-slate-300"
                >
                  <span>
                    Temporary chat by default
                    <span class="block text-xs font-normal text-slate-500"
                      >Image and Deep Research still force normal chat when
                      required.</span
                    >
                  </span>
                  <input
                    class="h-4 w-4"
                    type="checkbox"
                    bind:checked={temporaryChat}
                  />
                </label>
              </div>

              <div class="grid gap-3 lg:grid-cols-2">
                <Input label="API key" bind:value={serverKey} />
                <Input label="Public base URL" bind:value={serverPublicBase} />
              </div>
              <div class="rounded-3xl border border-white/10 bg-black/20 p-4">
                <div class="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <div class="text-sm font-black text-slate-100">
                      Output paths
                    </div>
                    <p class="mt-1 text-xs text-slate-500">
                      Paths stay editable because Docker volumes and LAN hosts
                      vary per machine.
                    </p>
                  </div>
                  <div class="flex flex-wrap gap-2">
                    <button
                      class="rounded-xl border border-white/10 bg-white/5 px-3 py-2 text-xs font-black"
                      onclick={() => quickOutputRoot("outputs")}
                    >
                      outputs
                    </button>
                    <button
                      class="rounded-xl border border-white/10 bg-white/5 px-3 py-2 text-xs font-black"
                      onclick={() => quickOutputRoot("/data/chatgpt-api")}
                    >
                      docker /data
                    </button>
                  </div>
                </div>
                <div class="mt-3 grid gap-3 lg:grid-cols-3">
                  <Input label="Image output dir" bind:value={imageOutputDir} />
                  <Input
                    label="Research output dir"
                    bind:value={researchOutputDir}
                  />
                  <Input label="Admin DB path" bind:value={adminDbPath} />
                </div>
              </div>
            </div>
            <CodeBlock
              title="Terminal 1: start bridge API"
              code={serveCommand}
            />
            <CodeBlock
              title="Terminal 2: start console"
              code={consoleCommand}
            />
            <CodeBlock
              title="Terminal 2 alternative: expose console on LAN"
              code={consoleLanCommand}
            />
          </article>
        </section>
      {/if}
    </main>

  {#if captureModalOpen}
    <div
      class="capture-modal-shell"
      role="presentation"
    >
      <button
        class="capture-modal-backdrop"
        aria-label="Close update capture modal"
        onclick={closeCaptureModal}
      ></button>
      <div
        class="capture-modal"
        role="dialog"
        aria-modal="true"
        aria-label={`Update capture for ${captureAccount}`}
      >
        <div class="capture-modal-head">
          <div class="min-w-0">
            <PanelTitle
              kicker="repair"
              title={`Update capture: ${captureAccount}`}
            />
            <p>
              Paste a fresh request from the same account when cookies, token,
              sentinel, or conduit values expire. Headers/Payload text and full
              Copy as cURL output are both accepted. cURL captures must include
              cookies through `Cookie:` or `-b`, Authorization, and `--data-raw`.
              The local account name stays the same.
            </p>
          </div>
          <button onclick={closeCaptureModal}>Close</button>
        </div>

        <Textarea
          label="Fresh conversation request capture"
          bind:value={captureText}
          rows={12}
        />
        <div class="capture-modal-actions">
          <button class="primary-cta" onclick={() => inspectExistingCapture(false)}>
            Inspect
          </button>
          <button onclick={() => inspectExistingCapture(true)}>
            Save update
          </button>
        </div>
        <CaptureResult result={captureResult} />
      </div>
    </div>
  {/if}

  {#if commandPaletteOpen}
    <div
      class="command-palette-backdrop"
      role="button"
      tabindex="0"
      aria-label="Close command palette"
      onclick={() => (commandPaletteOpen = false)}
      onkeydown={(event) => {
        if (event.key === "Enter" || event.key === " ") commandPaletteOpen = false;
      }}
    >
      <div
        class="command-palette"
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        tabindex="-1"
        onclick={(event) => event.stopPropagation()}
        onkeydown={(event) => event.stopPropagation()}
      >
        <div class="palette-search">
          <span>⌘K</span>
          <input
            bind:value={commandQuery}
            placeholder="Run command..."
          />
        </div>
        <div class="palette-list">
          {#each filteredCommandActions as action (action.id)}
            <button onclick={() => runCommandAction(action)}>
              <strong>{action.title}</strong>
              <small>{action.detail}</small>
            </button>
          {:else}
            <p>No matching command.</p>
          {/each}
        </div>
      </div>
    </div>
  {/if}

  {#if busy}
    <div
      class="fixed bottom-4 left-1/2 z-40 -translate-x-1/2 rounded-full border border-sky-300/30 bg-slate-950/95 px-4 py-2 text-sm text-sky-100 shadow-2xl"
    >
      Working: {busy}
    </div>
  {/if}

  {#if toast}
    <div
      class={`fixed bottom-4 right-4 z-50 max-w-lg rounded-2xl border px-4 py-3 shadow-2xl ${
        toastTone === "bad"
          ? "border-rose-300/40 bg-rose-950/90 text-rose-100"
          : "border-sky-300/40 bg-slate-950/95 text-sky-100"
      }`}
    >
      {toast}
    </div>
  {/if}
</div>
