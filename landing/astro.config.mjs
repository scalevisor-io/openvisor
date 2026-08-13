// @ts-check
import { defineConfig } from 'astro/config';
import sitemap from '@astrojs/sitemap';

// Static marketing site. SITE_URL is baked at build time (defaults to a
// placeholder origin); DEPLOY_DOMAIN allowlists the dev-server vhost.
const devDomain = process.env.DEPLOY_DOMAIN || 'openvisor.local';

export default defineConfig({
  site: process.env.SITE_URL || 'https://openvisor.example.com',
  integrations: [sitemap()],
  build: {
    inlineStylesheets: 'auto',
  },
  compressHTML: true,
  vite: {
    // Dev server only: let the Traefik vhost through (Vite blocks non-localhost hosts by default)
    server: {
      allowedHosts: [devDomain, `www.${devDomain}`],
    },
  },
});
