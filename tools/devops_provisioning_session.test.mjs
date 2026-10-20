// Pure launch declaration controls; no credentials, native or cloud execution.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { validateRuntimeDeclaration } from './devops_provisioning_session.mjs';

const canonical = value => JSON.stringify(Object.fromEntries(Object.entries(value).sort(([a], [b]) => a.localeCompare(b))));
const hash = value => createHash('sha256').update(canonical(value)).digest('hex');
function vector() {
  return { PATH: '/owned/verified-tools', AWS_REGION: 'us-west-2', AWS_ACCESS_KEY_ID: 'SOURCE_CONTROL_ONLY',
    AWS_SECRET_ACCESS_KEY: 'NOT_A_CREDENTIAL', AWS_SESSION_TOKEN: 'NOT_A_TOKEN',
    HONUA_DEVOPS_EXECUTION_MODE: 'execute', HONUA_DEVOPS_EXECUTION_TIER: 'execute-lower-env',
    HONUA_DEVOPS_APPROVAL_MODE: 'direct-allowed', HONUA_DEVOPS_ALLOWED_ENVIRONMENTS: 'it',
    HONUA_DEVOPS_TERRAFORM_LOCAL_PATH: '/owned/immutable-iac',
    HONUA_DEVOPS_PROVISION_APPROVAL_SIGNING_MODE: 'kms-mac',
    HONUA_DEVOPS_PROVISION_APPROVAL_ISSUER_KEY_ARNS: '{"sourceControlOnly":"not-a-key"}',
    HONUA_DEVOPS_CANDIDATE_REFERENCE: 'source-control-only',
    HONUA_DEVOPS_MCP_PROXY_PACKAGE: 'source-control-only', HONUA_DEVOPS_MCP_PROXY_INTEGRITY: 'source-control-only',
    TF_VAR_honua_admin_password: 'NOT_A_PASSWORD' };
}

test('exact privately declared normal context is copied without ambient environment', () => {
  const environment = vector();
  const observed = validateRuntimeDeclaration(environment, hash(environment), ['honua_admin_password']);
  assert.deepEqual(observed, environment); assert.notEqual(observed, environment);
  assert.equal(Object.hasOwn(observed, 'HOME'), false);
});

for (const mutation of ['extra', 'missing-session', 'offline', 'local-hmac', 'wrong-hash', 'unrequested-var', 'wrong-tier']) {
  test('runtime refuses ' + mutation + ' before intake or launch', () => {
    const environment = vector(); let expected = hash(environment);
    if (mutation === 'extra') environment.BASH_ENV = '/untrusted';
    if (mutation === 'missing-session') delete environment.AWS_SESSION_TOKEN;
    if (mutation === 'offline') environment.HONUA_IAC_OFFLINE = '1';
    if (mutation === 'local-hmac') environment.HONUA_DEVOPS_PROVISION_APPROVAL_SIGNING_MODE = 'local-hmac-dev';
    if (mutation === 'unrequested-var') environment.TF_VAR_other = 'not-declared';
    if (mutation === 'wrong-tier') environment.HONUA_DEVOPS_EXECUTION_TIER = 'observe';
    if (mutation !== 'wrong-hash') expected = hash(environment); else expected = 'f'.repeat(64);
    assert.throws(() => validateRuntimeDeclaration(environment, expected, ['honua_admin_password']));
  });
}
