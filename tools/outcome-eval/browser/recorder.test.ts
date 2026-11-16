import { expect, test, type Locator, type Page, type TestInfo } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { Recorder } from './recorder';

const VERDICT = { readback: 'independent readback' };
const SHORT_LIMIT_MINUTES = 1 / 60_000;

function recording(info: TestInfo) {
  const directory = info.outputPath('recording');
  let closes = 0;
  let navigations = 0;
  const page = {
    close: async () => { closes++; },
    goto: async () => { navigations++; },
  } as unknown as Page;
  return {
    recorder: new Recorder(page, directory),
    closes: () => closes,
    navigations: () => navigations,
    events: () => readFileSync(`${directory}/events.jsonl`, 'utf8').trim().split('\n').map(line => JSON.parse(line)),
  };
}

test('a failed dispatched click remains counted with its retry', async ({}, info) => {
  const { recorder, events } = recording(info);
  const locator = {
    click: async (options?: { trial: boolean }) => {
      if (!options?.trial) throw new Error('dispatch failed');
    },
  } as unknown as Locator;
  await expect(recorder.trial(1, async () => {
    await recorder.click(locator, 'Publish');
    return VERDICT;
  })).rejects.toThrow('dispatch failed');
  expect(events().map(event => event.type)).toEqual(['dispatch', 'click', 'retry', 'stop']);
  expect(events()[1].count).toBe(1);
  expect(events()[2].dispatched).toBe(true);
});

test('an unready click records a retry without a click', async ({}, info) => {
  const { recorder, events } = recording(info);
  const locator = { click: async () => { throw new Error('target unavailable'); } } as unknown as Locator;
  await expect(recorder.trial(1, async () => {
    await recorder.click(locator, 'Publish');
    return VERDICT;
  })).rejects.toThrow('target unavailable');
  expect(events().map(event => event.type)).toEqual(['dispatch', 'retry', 'stop']);
  expect(events()[1].dispatched).toBe(false);
});

test('timeout closes the page and prevents dispatch after pending readiness', async ({}, info) => {
  const { recorder, events, closes } = recording(info);
  let release!: () => void;
  const ready = new Promise<void>(resolve => { release = resolve; });
  let dispatches = 0;
  const locator = {
    click: async (options?: { trial: boolean }) => {
      if (options?.trial) await ready;
      else dispatches++;
    },
  } as unknown as Locator;
  let action!: Promise<void>;
  await expect(recorder.trial(SHORT_LIMIT_MINUTES, async () => {
    action = recorder.click(locator, 'Publish');
    await action;
    return VERDICT;
  })).rejects.toThrow('time limit');
  release();
  await expect(action).rejects.toThrow('the trial is not running');
  expect(closes()).toBe(1);
  expect(dispatches).toBe(0);
  expect(events().map(event => event.type)).toEqual(['dispatch', 'stop']);
  expect(events()[1].reason).toContain('time limit');
  await expect(recorder.goto('/late')).rejects.toThrow('the trial is not running');
});

test('timeout retains an in-flight dispatched click in the stopped transcript', async ({}, info) => {
  const { recorder, events, closes } = recording(info);
  let finish!: () => void;
  const pending = new Promise<void>(resolve => { finish = resolve; });
  const locator = {
    click: async (options?: { trial: boolean }) => { if (!options?.trial) await pending; },
  } as unknown as Locator;
  let work!: Promise<unknown>;
  await expect(recorder.trial(SHORT_LIMIT_MINUTES, () => {
    work = recorder.click(locator, 'Publish').then(() => recorder.goto('/late'));
    return work.then(() => VERDICT);
  })).rejects.toThrow('time limit');
  finish();
  await expect(work).rejects.toThrow('the trial is not running');
  expect(closes()).toBe(1);
  expect(events().map(event => event.type)).toEqual(['dispatch', 'click', 'stop']);
});

test('actions require a running trial and a recorder cannot be reused', async ({}, info) => {
  const { recorder, events, closes, navigations } = recording(info);
  await expect(recorder.goto('/early')).rejects.toThrow('the trial is not running');
  await recorder.trial(1, async () => VERDICT);
  await expect(recorder.goto('/late')).rejects.toThrow('the trial is not running');
  await expect(recorder.trial(1, async () => VERDICT)).rejects.toThrow('only one trial');
  expect(events().map(event => event.type)).toEqual(['dispatch', 'terminal-readback']);
  expect(closes()).toBe(0);
  expect(navigations()).toBe(0);
});

test('invalid time limits fail before dispatch', async ({}, info) => {
  const { recorder } = recording(info);
  for (const limit of [0, -1, NaN, Infinity]) {
    await expect(recorder.trial(limit, async () => VERDICT)).rejects.toThrow('positive and finite');
  }
  await recorder.trial(1, async () => VERDICT);
});
