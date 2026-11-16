import { expect, type Locator, type Page } from '@playwright/test';
import { appendFileSync, mkdirSync, writeFileSync } from 'node:fs';
import { performance } from 'node:perf_hooks';

export interface Verdict {
  readback: string;
  failure?: string;
}

export interface UploadedFile {
  name: string;
  mimeType: string;
  buffer: Buffer;
}

const NOTE_LIMIT = 300;

function firstLine(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error);
  return message.split('\n')[0].slice(0, NOTE_LIMIT);
}

interface Action {
  type: 'click' | 'field';
  target: string;
  fields: Record<string, unknown>;
}

const MILLISECONDS_PER_MINUTE = 60_000;

/**
 * Writes the raw events.jsonl transcript. A typed value is recorded as its length only.
 * An action is counted when it is dispatched; if it then fails, a retry event keeps the error.
 */
export class Recorder {
  private sequence = 0;
  private startedAt: number | undefined;
  private ended = false;
  private readonly file: string;

  constructor(readonly page: Page, directory: string) {
    mkdirSync(directory, { recursive: true });
    this.file = `${directory}/events.jsonl`;
    writeFileSync(this.file, '', { flag: 'wx' });
  }

  /** Runs the evaluated work between dispatch and exactly one end boundary, which the time limit forces. */
  async trial(limitMinutes: number, task: () => Promise<Verdict>): Promise<Verdict> {
    this.startedAt = performance.now();
    this.emit('dispatch');
    let timer: NodeJS.Timeout | undefined;
    const deadline = new Promise<never>((_, reject) => {
      const expire = () => reject(new Error(`the time limit of ${limitMinutes} minutes was reached`));
      timer = setTimeout(expire, limitMinutes * MILLISECONDS_PER_MINUTE);
    });
    try {
      const verdict = await Promise.race([task(), deadline]);
      if (verdict.failure) this.end('stop', { reason: verdict.failure.slice(0, NOTE_LIMIT) });
      else this.end('terminal-readback', { note: verdict.readback });
      return verdict;
    } catch (error) {
      this.end('stop', { reason: `exception: ${firstLine(error)}` });
      throw error;
    } finally {
      clearTimeout(timer);
    }
  }

  /** Marks grading reads, which add no action count. */
  observer(note: string) {
    this.emit('observer', { note: note.slice(0, NOTE_LIMIT) });
  }

  async goto(path: string) {
    try {
      await this.page.goto(path, { waitUntil: 'load' });
    } catch (error) {
      this.emit('retry', { failed: 'navigation', target: path, error: firstLine(error) });
      throw error;
    }
    this.emit('navigation', { target: path, how: 'address bar' });
  }

  async click(locator: Locator, target: string) {
    const action: Action = { type: 'click', target, fields: { count: 1, target } };
    await this.act(action, () => locator.click({ trial: true }), () => locator.click());
  }

  async fill(locator: Locator, fieldId: string, text: string) {
    const fields = { fieldId, characters: [...text].length, how: 'type' };
    await this.act({ type: 'field', target: fieldId, fields }, () => expect(locator).toBeEditable(), () => locator.fill(text));
  }

  async select(locator: Locator, fieldId: string, option: string | { label: string }) {
    const fields = { fieldId, characters: 0, how: 'select' };
    await this.act({ type: 'field', target: fieldId, fields }, () => expect(locator).toBeEnabled(), () => locator.selectOption(option));
  }

  /** One click opens the chooser and one field action picks the file. */
  async chooseFile(trigger: Locator, fieldId: string, file: UploadedFile) {
    const opened = this.page.waitForEvent('filechooser', { timeout: 15_000 });
    opened.catch(() => undefined);
    await this.click(trigger, `${fieldId} (open file chooser)`);
    const fields = { fieldId, characters: 0, how: 'file' };
    await this.act({ type: 'field', target: fieldId, fields }, () => opened, async () => (await opened).setFiles(file));
  }

  /** An action is counted when it is dispatched, whether or not it then completes; an unready target counts nothing. */
  private async act(action: Action, ready: () => Promise<unknown>, dispatch: () => Promise<unknown>) {
    let dispatched = false;
    try {
      await ready();
      dispatched = true;
      this.emit(action.type, action.fields);
      await dispatch();
    } catch (error) {
      this.emit('retry', { failed: action.type, target: action.target, dispatched, error: firstLine(error) });
      throw error;
    }
  }

  private end(type: 'stop' | 'terminal-readback', fields: Record<string, unknown>) {
    this.emit(type, fields);
    this.ended = true;
  }

  private emit(type: string, fields: Record<string, unknown> = {}) {
    if (this.ended) return;
    const elapsed = this.startedAt === undefined ? 0 : performance.now() - this.startedAt;
    const event = {
      id: `e${String(++this.sequence).padStart(4, '0')}`,
      seconds: Math.round(elapsed / 10) / 100,
      type,
      ...fields,
    };
    appendFileSync(this.file, `${JSON.stringify(event)}\n`);
  }
}
