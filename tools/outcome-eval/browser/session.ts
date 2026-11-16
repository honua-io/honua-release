import { readFileSync } from 'node:fs';
import type { Recorder } from './recorder';

function required(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is required`);
  return value;
}

export const outputDirectory = () => required('OUTCOME_OUT');
export const apiUrl = () => required('OUTCOME_API_URL');
export const repetition = () => process.env.OUTCOME_REPETITION ?? 'dev';

export function operatorPassword(): string {
  const password = readFileSync(required('OUTCOME_PASSWORD_FILE'), 'utf8').trim();
  if (!password) throw new Error('The operator password file is empty');
  return password;
}

/** A short name that keeps repeated trials apart on one install. */
export function trialName(prefix: string): string {
  return `${prefix}${repetition()}${Date.now().toString(36).slice(-4)}`;
}

/** Signs in as a person does: one navigation, one typed field and one click. */
export async function signIn(recorder: Recorder) {
  const page = recorder.page;
  await recorder.goto('/console/auth/signin');
  const password = page.locator('input[type="password"]');
  await password.waitFor({ timeout: 60_000 });
  await recorder.fill(password, 'sign-in.password', operatorPassword());
  await recorder.click(page.getByRole('button', { name: 'Continue', exact: true }), 'Continue');
  await page.waitForURL(url => !url.pathname.includes('/auth/signin'), { timeout: 60_000 });
}

/** Opens a top navigation group and follows one of its links: two clicks. */
export async function openMenu(recorder: Recorder, group: string, link: string) {
  const page = recorder.page;
  const groups = page.locator('header summary, nav summary');
  const links = page.locator('header a, nav a');
  await recorder.click(groups.filter({ hasText: new RegExp(`^${group}`) }).first(), `menu ${group}`);
  await recorder.click(links.filter({ hasText: new RegExp(`^${link}`) }).first(), `menu link ${link}`);
  await page.waitForLoadState('load');
}
