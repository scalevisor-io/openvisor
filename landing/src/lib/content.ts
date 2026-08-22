// Build-time content loader for the white-label landing. Reads the committed
// site.example.yml (neutral template copy) or a gitignored site.yml override when
// present (wholesale replacement, no merge), applies {{BRAND_NAME}} /
// {{CONSULTANT_NAME}} substitution from build-time env, and returns a typed,
// validated SiteContent. Node-only: it touches the filesystem, so it must never
// be imported into client-side code. Missing required keys fail the build loudly.
import fs from 'node:fs';
import path from 'node:path';
import yaml from 'js-yaml';
import { brandName, consultantName } from './site';

export interface Offer {
  name: string;
  priceCurrency: string;
  description: string;
}

export interface Phase {
  chip: { label: string; tone: string; indicator: string };
  stages: string[];
  events: { icon: string; body: string; meta: string }[];
}

export interface FeatureRequest {
  icon: string;
  body: string;
  meta: string;
}

export interface PlatformPoint {
  icon: string;
  title: string;
  surface: string;
  caption: string;
}

export interface TrustPoint {
  icon: string;
  strong: string;
  text: string;
}

export interface Step {
  n: string;
  title: string;
  body: string;
  who?: string;
}

export interface ProgramLine {
  g: string;
  text: string;
  tone?: string;
}

export interface ProgramExample {
  monogram: string;
  from: string;
  to: string;
  title: string;
  mode: string;
  body: string;
  meta: string;
  run: { id: string; lines: ProgramLine[] };
}

export interface WayCard {
  tag: string;
  title: string;
  lede: string;
  list: string[];
  forLabel: string;
  forText: string;
  cta: string;
}

export interface PriceCard {
  tag: string;
  title: string;
  body: string;
}

export interface SiteContent {
  seo: {
    title: string;
    description: string;
    themeColor: string;
    ogSiteName: string;
    orgName: string;
    orgDescription: string;
    sameAs: string[];
    serviceType: string;
    serviceDescription: string;
    serviceAreaServed: string;
    offers: Offer[];
  };
  legal: {
    entity: string;
    // Optional: the operating company's registered address. The admin Settings
    // page overrides both this and `entity` at runtime (see layouts/Base.astro).
    address?: string;
    consultant: string;
    contactEmail: string;
    privacyUpdated: string;
    termsUpdated: string;
  };
  footer: {
    tagline: string;
  };
  hero: {
    eyebrow: string[];
    titleLead: string;
    titleAccent: string;
    lede: string;
    ctaPrimary: string;
    ctaSecondary: string;
    note: string;
    visualName: string;
    visualCaption: string;
  };
  specs: string[];
  stageLabels: string[];
  phases: Phase[];
  featureRequests: FeatureRequest[];
  twoWays: {
    eyebrow: string;
    title: string;
    intro: string;
    primary: WayCard;
    secondary: WayCard;
    note: string;
  };
  projects: {
    eyebrow: string;
    title: string;
    intro: string;
    points: PlatformPoint[];
  };
  programs: {
    eyebrow: string;
    title: string;
    intro: string;
    examples: ProgramExample[];
    cta: string;
  };
  how: {
    eyebrow: string;
    title: string;
    intro: string;
    steps: Step[];
  };
  pricing: {
    eyebrow: string;
    title: string;
    intro: string;
    primary: PriceCard;
    secondary: PriceCard;
    cta: string;
  };
  sovereign: {
    eyebrow: string;
    title: string;
    intro: string;
    trustPoints: TrustPoint[];
  };
  finalCta: {
    titleLead: string;
    titleAccent: string;
    lede: string;
    ctaPrimary: string;
    ctaSecondaryLabel: string;
    ctaSecondaryHref: string;
  };
  // The /llms.txt body. Brand + consultant are already substituted; the
  // {{SITE_URL}} / {{APP_URL}} markers are filled by the llms.txt endpoint.
  llms: string;
}

// Anchored to the project root (cwd under both `astro dev` and `astro build`)
// rather than import.meta.url, which Vite rewrites to the bundled chunk path in
// the static build - the YAML is not co-located with it there.
const dataDir = path.join(process.cwd(), 'src', 'data');
const overridePath = path.join(dataDir, 'site.yml');
const examplePath = path.join(dataDir, 'site.example.yml');
const sourcePath = fs.existsSync(overridePath) ? overridePath : examplePath;

let raw = fs.readFileSync(sourcePath, 'utf8');
raw = raw
  .replaceAll('{{BRAND_NAME}}', brandName)
  .replaceAll('{{CONSULTANT_NAME}}', consultantName);

const data = yaml.load(raw) as SiteContent;

// Fail the build loudly if the YAML is missing a top-level section, so a broken
// or half-written site.yml never ships a silently empty page.
const required: (keyof SiteContent)[] = [
  'seo',
  'legal',
  'footer',
  'hero',
  'specs',
  'stageLabels',
  'phases',
  'featureRequests',
  'twoWays',
  'projects',
  'programs',
  'how',
  'pricing',
  'sovereign',
  'finalCta',
  'llms',
];
if (!data || typeof data !== 'object') {
  throw new Error(`content: ${sourcePath} did not parse to an object`);
}
const missing = required.filter((key) => data[key] == null);
if (missing.length) {
  throw new Error(`content: ${sourcePath} is missing required keys: ${missing.join(', ')}`);
}

export const site: SiteContent = data;
export default site;
