import { metaApi } from "./endpoints";
import type { QuestionsDoc, Speciality } from "../types";

// Module-level caches - these datasets are static per deploy.
let specialitiesCache: Promise<Speciality[]> | null = null;
let questionsCache: Promise<QuestionsDoc> | null = null;

export function loadSpecialities(): Promise<Speciality[]> {
  if (!specialitiesCache) specialitiesCache = metaApi.specialities();
  return specialitiesCache;
}

export function loadQuestions(): Promise<QuestionsDoc> {
  if (!questionsCache) questionsCache = metaApi.questions();
  return questionsCache;
}

export function specialityLabel(specialities: Speciality[], id: string | null): string {
  if (!id) return "-";
  return specialities.find((s) => s.id === id)?.short_label ?? id;
}
