interface PauseCommand {
  command_id: string;
  revision: number;
}

export class PauseGate {
  paused = false;
  private operation: Promise<PauseCommand> | undefined;
  private command: PauseCommand | undefined;

  constructor(private readonly clear: () => void, private readonly mute: () => Promise<void>) {}

  pause(value: PauseCommand): Promise<PauseCommand> {
    if (!/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(value.command_id) ||
        !Number.isSafeInteger(value.revision) || value.revision < 1) {
      return Promise.reject(new Error("Invalid pause correlation."));
    }
    if (this.command) {
      if (this.command.command_id !== value.command_id || this.command.revision !== value.revision) {
        return Promise.reject(new Error("Pause is latched; a different correlation cannot replace it."));
      }
      return this.operation!;
    }
    this.command = { command_id: value.command_id, revision: value.revision };
    this.paused = true;
    this.clear();
    this.operation = this.mute().then(() => {
      this.clear();
      return this.command!;
    });
    return this.operation;
  }
}
