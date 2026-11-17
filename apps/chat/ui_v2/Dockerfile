# Build stage
FROM node:20-alpine AS builder

WORKDIR /app

# Copy package files
COPY package*.json ./

# Install dependencies
RUN npm ci

# Copy source code
COPY . .

# NEXT_PUBLIC_* are inlined at build time. azd supplies this environment's URLs.
ARG NEXT_PUBLIC_API_URL=http://localhost:8000
ENV NEXT_PUBLIC_API_URL=${NEXT_PUBLIC_API_URL}

# Voice (realtime) backend URL — inlined for the in-browser voice panel
# (useRealtimeVoice -> /realtime WS, Call a phone -> /call) so it targets ca-voice.
ARG NEXT_PUBLIC_VOICE_URL=http://localhost:8002
ENV NEXT_PUBLIC_VOICE_URL=${NEXT_PUBLIC_VOICE_URL}

# Build the application by running next directly via node
RUN node node_modules/next/dist/bin/next build

# Production stage
FROM node:20-alpine AS runner

WORKDIR /app

# Set environment to production
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1

# Create non-root user
RUN addgroup --system --gid 1001 nodejs
RUN adduser --system --uid 1001 nextjs

# Copy built files
COPY --from=builder --chown=nextjs:nodejs /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

# Set correct permissions
USER nextjs

# Expose port
EXPOSE 8080

ENV PORT=8080
ENV HOSTNAME="0.0.0.0"

# Start the application
CMD ["node", "server.js"]
