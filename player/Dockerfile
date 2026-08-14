FROM node:22-alpine AS assets

WORKDIR /build

# Pin Video.js so rebuilding the image cannot silently change the player.
RUN npm install --omit=dev --no-audit --no-fund video.js@8.23.7

FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY index.html /usr/share/nginx/html/index.html
COPY --from=assets /build/node_modules/video.js/dist /usr/share/nginx/html/vendor/video.js

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD wget --quiet --spider http://127.0.0.1/ || exit 1
