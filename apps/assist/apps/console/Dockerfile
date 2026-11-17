# Build stage
FROM node:20-alpine AS builder

WORKDIR /app

# Copy root package files
COPY package.json package-lock.json ./
COPY apps/console/package.json ./apps/console/
COPY packages/shared/package.json ./packages/shared/

# Install dependencies
RUN npm ci

# Copy source code
COPY packages/shared ./packages/shared
COPY apps/console ./apps/console

# Build shared package
WORKDIR /app/packages/shared
RUN npm run build

# Build Next.js app
WORKDIR /app/apps/console
# NEXT_PUBLIC_* are inlined at build time; azd supplies this environment's API.
ARG NEXT_PUBLIC_ORCHESTRATOR_URL=http://localhost:8000
ENV NEXT_PUBLIC_ORCHESTRATOR_URL=${NEXT_PUBLIC_ORCHESTRATOR_URL}
RUN npm run build

# Production stage
FROM node:20-alpine AS runner

WORKDIR /app

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1

# Create non-root user
RUN addgroup --system --gid 1001 nodejs
RUN adduser --system --uid 1001 nextjs

# Copy the standalone output (preserves monorepo structure)
COPY --from=builder --chown=nextjs:nodejs /app/apps/console/.next/standalone ./
# Copy static files to the correct location within the standalone structure
COPY --from=builder --chown=nextjs:nodejs /app/apps/console/.next/static ./apps/console/.next/static
# Copy public folder
COPY --from=builder --chown=nextjs:nodejs /app/apps/console/public ./apps/console/public

USER nextjs

EXPOSE 3000

ENV PORT=3000
ENV HOSTNAME="0.0.0.0"

# The server.js is at the app location in monorepo structure
CMD ["node", "apps/console/server.js"]
