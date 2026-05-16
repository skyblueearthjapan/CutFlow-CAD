# CutFlow•CAD frontend — Vite dev server (Phase 0)
FROM node:20-alpine

WORKDIR /app

# package.json + lock を先にコピーしてキャッシュを効かせる (ci で再現性確保)
COPY package.json package-lock.json ./
RUN npm ci

# ソースを後からコピー (dev では bind mount で上書き想定)
COPY . .

EXPOSE 5173

CMD ["npm", "run", "dev"]
