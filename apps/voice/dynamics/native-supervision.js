(function (root) {
  "use strict";
  const TABLE = "moacc_nativeaicommand";
  const PRIMARY = TABLE + "id";
  const STORAGE = "moacc-native-pause-requests";
  const UUID = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i;
  function conversationGuid(value) {
    if (typeof value !== "string") return null;
    const plain = value.replace(/^\{([0-9a-f-]{36})\}$/i, "$1");
    return UUID.test(plain) && plain !== "00000000-0000-0000-0000-000000000000" ? plain.toLowerCase() : null;
  }
  function recordNotFound(error) {
    return error?.status === 404 || error?.statusCode === 404 ||
      [0x80040217, -2147220969].includes(Number(error?.errorCode));
  }
  const SELECT = "?$select=" + [
    PRIMARY, "moacc_conversationid", "moacc_action", "moacc_outcome",
    "moacc_ackrevision", "moacc_ackon", "moacc_inflight"
  ].join(",");
  const text = {
    en: {
      title: "Pause AI", language: "العربية", button: "Pause AI / check request",
      detail: "For the focused native conversation only. Stops new AI listening, speech and tools; existing submitted operations may still finish. This does not join, transfer or end the call. AI will not resume automatically.",
      ready: "Ready. Monitoring alone does not pause AI.",
      requested: "Requested — not yet paused.", pausing: "Pausing — waiting for confirmed playback stop.",
      paused: "AI paused — input, output and new tools are blocked.",
      pendingTools: " An already-started operation may still finish or need reconciliation.",
      ended: "The native AI session has ended. This is not a new pause confirmation.",
      rejected: "Request rejected. Check the active conversation and approved access.",
      failed: "Pause could not be confirmed. Use native call controls; do not assume AI is silent.",
      unconfirmed: "Outcome not confirmed. Check this same request again; do not assume success.",
      changed: "Conversation focus changed. Return to the original conversation to check its request.",
      unavailable: "Open this control in an authorized native conversation session.",
      confirm: "Pause AI in the currently focused native conversation? This will not join or transfer the call."
    },
    ar: {
      title: "إيقاف المساعد مؤقتاً", language: "English", button: "إيقاف المساعد / التحقق من الطلب",
      detail: "للمحادثة الأصلية المحددة فقط. يوقف استماع المساعد وكلامه وأدواته الجديدة؛ وقد تكتمل عملية أُرسلت سابقاً. لا ينضم إلى المكالمة ولا يحولها ولا ينهيها، ولن يعود المساعد تلقائياً.",
      ready: "جاهز. المراقبة وحدها لا توقف المساعد.",
      requested: "تم طلب الإيقاف — لم يتأكد الإيقاف بعد.", pausing: "جارٍ الإيقاف — بانتظار تأكيد توقف الصوت.",
      paused: "تم إيقاف المساعد — حُجبت المدخلات والمخرجات والأدوات الجديدة.",
      pendingTools: " قد تكتمل عملية بدأت سابقاً أو تحتاج نتيجتها إلى التحقق.",
      ended: "انتهت جلسة المساعد الأصلية. هذه ليست موافقة على طلب إيقاف جديد.",
      rejected: "رُفض الطلب. تحقق من المحادثة النشطة والصلاحيات المعتمدة.",
      failed: "لم يتأكد الإيقاف. استخدم أدوات المكالمة الأصلية ولا تفترض أن المساعد صامت.",
      unconfirmed: "النتيجة غير مؤكدة. تحقق من الطلب نفسه مجدداً ولا تفترض نجاحه.",
      changed: "تغيرت المحادثة المحددة. عد إلى المحادثة السابقة للتحقق من طلبها.",
      unavailable: "افتح هذا التحكم ضمن جلسة محادثة أصلية مخوّلة.",
      confirm: "هل تريد إيقاف المساعد في المحادثة الأصلية المحددة؟ لن ينضم هذا الإجراء إلى المكالمة ولن يحولها."
    }
  };

  class PauseControl {
    constructor(host, options = {}) {
      this.host = host;
      this.show = options.show || (() => {});
      this.storage = options.storage;
      this.sleep = options.sleep || (ms => new Promise(resolve => setTimeout(resolve, ms)));
      this.uuid = options.uuid || (() => root.crypto.randomUUID());
      this.confirm = options.confirm || (() => true);
      this.now = options.now || (() => Date.now());
      this.busy = false;
      this.last = undefined;
      this.stopped = false;
    }
    focus() {
      const session = this.host.Microsoft.Apm.getFocusedSession();
      if (!session || session.isDefault || typeof session.sessionId !== "string" ||
          !session.sessionId || session.sessionId.length > 256) throw new Error("context");
      return session.sessionId;
    }
    sameFocus(session) {
      try { return !this.stopped && this.focus() === session; } catch { return false; }
    }
    storageGet(key) {
      try {
        const rows = JSON.parse(this.storage?.getItem(STORAGE) || "[]");
        return Array.isArray(rows) ? rows.slice(-16).find(row => row?.key === key)?.value : null;
      } catch { return null; }
    }
    storageSet(key, value) {
      try {
        const saved = JSON.parse(this.storage?.getItem(STORAGE) || "[]");
        const rows = Array.isArray(saved) ? saved.slice(-16).filter(row =>
          typeof row?.key === "string" && row.key.length < 400 && UUID.test(row.value) && row.key !== key
        ).slice(-15) : [];
        this.storage?.setItem(STORAGE, JSON.stringify([...rows, { key, value }]));
      } catch { /* Storage is only a bounded retry convenience. */ }
    }
    async bounded(operation, milliseconds = 8000) {
      let timer;
      try {
        return await Promise.race([
          operation,
          new Promise((_, reject) => { timer = setTimeout(() => reject(new Error("unconfirmed")), Math.max(1, milliseconds)); })
        ]);
      } finally { clearTimeout(timer); }
    }
    async request() {
      if (this.busy) return;
      this.busy = true;
      try {
        const deadline = this.now() + 45000;
        const session = this.focus();
        const conversation = await this.bounded(this.host.Microsoft.Omnichannel.getConversationId());
        if (!this.sameFocus(session)) { this.show("changed"); return; }
        const id = conversationGuid(conversation);
        if (!id) throw new Error("context");
        const api = this.host.Xrm.WebApi.online;
        if (!api?.createRecord || !api?.retrieveRecord) throw new Error("context");
        const key = "moacc-native-pause:" + session + ":" + id;
        const previous = this.storageGet(key);
        const command = previous && UUID.test(previous) ? previous.toLowerCase() : this.uuid().toLowerCase();
        if (!UUID.test(command)) throw new Error("context");
        if (!previous && !this.confirm()) return;
        if (!this.sameFocus(session)) { this.show("changed"); return; }
        if (this.now() >= deadline) { this.show("unconfirmed"); return; }
        this.storageSet(key, command);
        this.last = { session, conversation: id, command };
        this.show("requested");
        let createAttempted = false;
        const createSameCommand = async () => {
          if (!this.sameFocus(session)) { this.show("changed"); return false; }
          if (this.now() >= deadline) { this.show("unconfirmed"); return false; }
          createAttempted = true;
          try {
            await this.bounded(api.createRecord(TABLE, { [PRIMARY]: command, moacc_conversationid: id, moacc_action: "pause" }));
          } catch {
            // A lost create response is ambiguous. Read the same ID; never create a replacement.
          }
          return true;
        };
        if (!previous && !await createSameCommand()) return;
        for (let attempt = 0; attempt < 45 && this.now() < deadline; attempt++) {
          if (!this.sameFocus(session)) { this.show("changed"); return; }
          let row;
          try { row = await this.bounded(api.retrieveRecord(TABLE, command, SELECT), Math.min(8000, deadline - this.now())); }
          catch (error) {
            if (!this.sameFocus(session)) { this.show("changed"); return; }
            if (!recordNotFound(error) || createAttempted) { this.show("unconfirmed"); return; }
            // An explicit later click may retry an absent row once, using its original primary key.
            if (!await createSameCommand()) return;
            continue;
          }
          if (!this.sameFocus(session)) { this.show("changed"); return; }
          if (this.now() >= deadline) { this.show("unconfirmed"); return; }
          if (row[PRIMARY] !== command || row.moacc_conversationid !== id || row.moacc_action !== "pause") {
            this.show("failed"); return;
          }
          const outcome = row.moacc_outcome;
          if (outcome === "paused") {
            if (!Number.isSafeInteger(row.moacc_ackrevision) || row.moacc_ackrevision < 1 ||
                typeof row.moacc_ackon !== "string" || !Number.isFinite(Date.parse(row.moacc_ackon))) {
              this.show("unconfirmed"); return;
            }
            this.show("paused", Number(row.moacc_inflight) > 0);
            return;
          }
          if (["rejected", "failed", "ended"].includes(outcome)) { this.show(outcome, Number(row.moacc_inflight) > 0); return; }
          if (outcome !== undefined && outcome !== null && !["", "pending", "processing"].includes(outcome)) {
            this.show("unconfirmed"); return;
          }
          this.show(outcome === "processing" ? "pausing" : "requested");
          await this.sleep(Math.max(0, Math.min(1000, deadline - this.now())));
        }
        this.show("unconfirmed");
      } catch {
        this.show("unavailable");
      } finally {
        this.busy = false;
      }
    }
  }

  function boot(document, host) {
    let language = host.Xrm?.Utility?.getGlobalContext()?.userSettings?.languageId === 1025 ? "ar" : "en";
    let status = "ready", pending = false;
    const render = () => {
      document.documentElement.lang = language;
      document.documentElement.dir = language === "ar" ? "rtl" : "ltr";
      for (const [id, key] of [["pause-title", "title"], ["pause-detail", "detail"], ["pause-ai", "button"], ["pause-language", "language"]]) {
        document.getElementById(id).textContent = text[language][key];
      }
      document.getElementById("pause-status").textContent = text[language][status] + (pending ? text[language].pendingTools : "");
    };
    const control = new PauseControl(host, {
      storage: root.sessionStorage,
      show: (value, inflight = false) => { status = value; pending = inflight; render(); },
      confirm: () => root.confirm(text[language].confirm)
    });
    document.getElementById("pause-language").onclick = () => { language = language === "ar" ? "en" : "ar"; render(); };
    document.getElementById("pause-ai").onclick = () => control.request();
    try { control.focus(); document.getElementById("pause-ai").disabled = false; }
    catch { status = "unavailable"; }
    root.addEventListener("pagehide", () => { control.stopped = true; });
    render();
    return control;
  }
  const exported = { PauseControl, boot, text };
  if (typeof module !== "undefined" && module.exports) module.exports = exported;
  else {
    root.MoaccNativePause = exported;
    if (root.document?.getElementById("pause-ai")) {
      try { boot(root.document, root.parent); }
      catch { root.document.getElementById("pause-status").textContent = text.en.unavailable + " / " + text.ar.unavailable; }
    }
  }
})(globalThis);
