'use client';

import { useEffect, useRef, type MutableRefObject } from 'react';
import { template } from '@/config/template';

interface VoiceOrbProps {
  /** 0..1 audio level, read every frame (no re-render). */
  levelRef: MutableRefObject<number>;
  size?: number;
}

/**
 * A fluid, Siri-like audio-reactive orb rendered on canvas. The blob morphs with layered
 * sine wobble and swells with the live audio level; a soft gradient glow sits behind it.
 */
export function VoiceOrb({ levelRef, size = 320 }: VoiceOrbProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = size * dpr;
    canvas.height = size * dpr;
    ctx.scale(dpr, dpr);

    const cx = size / 2;
    const cy = size / 2;
    const points = 110;
    let raf = 0;
    let t = 0;

    const render = () => {
      t += 0.016;
      const level = Math.max(0, Math.min(1, levelRef.current));
      const breathe = (Math.sin(t * 1.1) + 1) * 0.5; // 0..1 gentle idle motion
      const base = size * 0.2;
      const amp = size * (0.03 + breathe * 0.012) + level * size * 0.16;

      ctx.clearRect(0, 0, size, size);

      // Outer glow
      const glow = ctx.createRadialGradient(cx, cy, base * 0.2, cx, cy, base + amp + size * 0.2);
      glow.addColorStop(0, template.brand.colors.primary);
      glow.addColorStop(0.45, template.brand.colors.accent);
      glow.addColorStop(1, 'transparent');
      ctx.globalAlpha = 0.34 + level * 0.4;
      ctx.fillStyle = glow;
      ctx.fillRect(0, 0, size, size);
      ctx.globalAlpha = 1;

      // Morphing blob
      ctx.beginPath();
      for (let i = 0; i <= points; i++) {
        const a = (i / points) * Math.PI * 2;
        const wobble =
          Math.sin(a * 3 + t * 1.6) * 0.5 +
          Math.sin(a * 5 - t * 1.1) * 0.3 +
          Math.sin(a * 2 + t * 0.7) * 0.2;
        const r = base + amp * 0.6 + wobble * amp * (0.5 + level);
        const x = cx + Math.cos(a) * r;
        const y = cy + Math.sin(a) * r;
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }
      ctx.closePath();

      const fill = ctx.createLinearGradient(cx - base, cy - base, cx + base, cy + base);
      fill.addColorStop(0, template.brand.colors.primary);
      fill.addColorStop(0.5, template.brand.colors.accent);
      fill.addColorStop(1, template.brand.colors.accent);
      ctx.fillStyle = fill;
      ctx.globalAlpha = 0.92;
      ctx.fill();

      // Inner highlight (glossy macOS sheen)
      const hl = ctx.createRadialGradient(cx - base * 0.3, cy - base * 0.35, 2, cx, cy, base * 1.1);
      hl.addColorStop(0, 'rgba(255, 255, 255, 0.85)');
      hl.addColorStop(1, 'rgba(255, 255, 255, 0)');
      ctx.globalAlpha = 0.28 + level * 0.25;
      ctx.fillStyle = hl;
      ctx.fill();
      ctx.globalAlpha = 1;

      raf = requestAnimationFrame(render);
    };

    raf = requestAnimationFrame(render);
    return () => cancelAnimationFrame(raf);
  }, [levelRef, size]);

  return <canvas ref={canvasRef} style={{ width: size, height: size }} className="select-none" />;
}
