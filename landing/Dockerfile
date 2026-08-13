# syntax=docker/dockerfile:1

# ---- Build stage -------------------------------------------------------
FROM node:20-alpine AS build
WORKDIR /app

# Install dependencies against the lockfile-free manifest.
# (No package-lock.json is committed; use npm install for a reproducible-enough build.)
COPY package.json ./
RUN npm install --no-audit --no-fund

# Build the static site. These build args bake the white-label identity into
# the HTML; unset, each falls back to the Openvisor default (src/lib/site.ts,
# astro.config.mjs, and src/data/site.example.yml). Rebuild after changing them.
ARG APP_URL
ARG SITE_URL
ARG BRAND_NAME
ARG CONSULTANT_NAME
ARG BRAND_COLOR_PRIMARY
ARG BRAND_COLOR_SECONDARY
ENV APP_URL=$APP_URL \
    SITE_URL=$SITE_URL \
    BRAND_NAME=$BRAND_NAME \
    CONSULTANT_NAME=$CONSULTANT_NAME \
    BRAND_COLOR_PRIMARY=$BRAND_COLOR_PRIMARY \
    BRAND_COLOR_SECONDARY=$BRAND_COLOR_SECONDARY
COPY . .
RUN npm run build

# ---- Serve stage -------------------------------------------------------
FROM nginx:alpine AS runtime
RUN rm -rf /usr/share/nginx/html/*
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD wget -q -O /dev/null http://127.0.0.1/ || exit 1
CMD ["nginx", "-g", "daemon off;"]
