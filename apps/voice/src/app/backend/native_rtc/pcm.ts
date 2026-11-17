export class PcmQueue {
  private nextTime = 0;
  private sources = new Set<AudioBufferSourceNode>();
  frames = 0;

  constructor(
    private readonly context: AudioContext,
    private readonly destination: AudioNode,
    private readonly sampleRate = 24000,
  ) {}

  enqueue(data: ArrayBuffer): void {
    if (data.byteLength === 0 || data.byteLength % 2 || data.byteLength > this.sampleRate * 2) {
      throw new Error("Invalid PCM16 frame size.");
    }
    if (this.context.state !== "running") {
      throw new Error("Audio context is not running.");
    }
    const now = this.context.currentTime;
    const start = Math.max(this.nextTime, now + 0.06);
    if (start - now + data.byteLength / (this.sampleRate * 2) > 0.5 + Number.EPSILON * 16) {
      throw new Error("Native audio queue exceeded 500 ms; refusing to drop or delay speech silently.");
    }
    const input = new DataView(data);
    const buffer = this.context.createBuffer(1, data.byteLength / 2, this.sampleRate);
    const output = buffer.getChannelData(0);
    for (let index = 0; index < output.length; index++) {
      output[index] = input.getInt16(index * 2, true) / 32768;
    }
    const source = this.context.createBufferSource();
    source.buffer = buffer;
    source.connect(this.destination);
    source.onended = () => {
      this.sources.delete(source);
      source.disconnect();
    };
    source.start(start);
    this.sources.add(source);
    this.nextTime = start + output.length / this.sampleRate;
    this.frames++;
  }

  clear(): void {
    for (const source of this.sources) {
      source.onended = null;
      source.stop();
      source.disconnect();
    }
    this.sources.clear();
    this.nextTime = 0;
  }
}
