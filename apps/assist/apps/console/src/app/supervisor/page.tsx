import { AssistUnavailable, TemplateBrand } from "@/components/template-brand";
import { template } from "@/config/template";

export default function SupervisorPage() {
  if (!template.features.agentAssist) return <AssistUnavailable />;
  return (
    <div className="flex h-screen flex-col">
      {/* Header */}
      <header className="flex h-14 items-center justify-between border-b px-6">
        <div className="flex items-center gap-4">
          <TemplateBrand />
          <div className="h-6 w-px bg-border" />
          <h1 className="text-lg font-semibold">Supervisor Dashboard</h1>
        </div>
        <div className="text-sm text-muted-foreground">
          Service supervision
        </div>
      </header>

      {/* Main Content */}
      <div className="flex-1 overflow-auto p-6">
        <div className="grid gap-6 md:grid-cols-2 lg:grid-cols-3">
          {/* Active Conversations Card */}
          <div className="rounded-lg border p-6">
            <h3 className="text-sm font-medium text-muted-foreground">
              Active Conversations
            </h3>
            <p className="mt-2 text-3xl font-bold">0</p>
          </div>

          {/* Agents Online Card */}
          <div className="rounded-lg border p-6">
            <h3 className="text-sm font-medium text-muted-foreground">
              Agents Online
            </h3>
            <p className="mt-2 text-3xl font-bold">0</p>
          </div>

          {/* Average Handle Time Card */}
          <div className="rounded-lg border p-6">
            <h3 className="text-sm font-medium text-muted-foreground">
              Avg Handle Time
            </h3>
            <p className="mt-2 text-3xl font-bold">--:--</p>
          </div>
        </div>

        {/* Conversations Table Placeholder */}
        <div className="mt-6 rounded-lg border">
          <div className="border-b p-4">
            <h2 className="font-medium">Live Conversations</h2>
          </div>
          <div className="p-8 text-center text-muted-foreground">
            No active conversations to monitor
          </div>
        </div>
      </div>
    </div>
  );
}
