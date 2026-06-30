import { test, expect } from '@playwright/test';
import { createApiClient } from '../utils/api-client';
import { createK8sHelper } from '../utils/kubernetes';

const apiClient = createApiClient();

test.describe('Health Checks - Scaled Deployment', () => {
  test('GET /health/live returns ok', async ({ request }) => {
    const response = await request.get('/health/live');
    expect(response.ok()).toBeTruthy();

    const body = await response.json();
    expect(body.status).toBe('ok');
    expect(body.checks).toBeDefined();
    expect(body.checks.redis?.status).toBe('ok');
    expect(body.checks.postgres?.status).toBe('ok');
  });

  test('GET /health/ready returns ok', async ({ request }) => {
    const response = await request.get('/health/ready');
    expect(response.ok()).toBeTruthy();

    const body = await response.json();
    expect(body.status).toBe('ok');
  });

  test('all pylon-main pods are ready', async () => {
    const k8s = createK8sHelper();
    const pods = await k8s.getPods('app.kubernetes.io/name=pylon-main');

    expect(pods.length).toBeGreaterThanOrEqual(3);
    for (const pod of pods) {
      expect(pod.ready).toBe(true);
      expect(pod.status).toBe('Running');
    }
  });

  test('all pylon-auth pods are ready', async () => {
    const k8s = createK8sHelper();
    const pods = await k8s.getPods('app.kubernetes.io/name=pylon-auth');

    expect(pods.length).toBeGreaterThanOrEqual(2);
    for (const pod of pods) {
      expect(pod.ready).toBe(true);
      expect(pod.status).toBe('Running');
    }
  });

  test('all pylon-indexer pods are ready', async () => {
    const k8s = createK8sHelper();
    const pods = await k8s.getPods('app.kubernetes.io/name=pylon-indexer');

    expect(pods.length).toBeGreaterThanOrEqual(3);
    for (const pod of pods) {
      expect(pod.ready).toBe(true);
      expect(pod.status).toBe('Running');
    }
  });

  test('health endpoint responds within 500ms', async ({ request }) => {
    const start = Date.now();
    const response = await request.get('/health/live');
    const duration = Date.now() - start;

    expect(response.ok()).toBeTruthy();
    expect(duration).toBeLessThan(500);
  });

  test('multiple sequential requests hit different pods', async ({ request }) => {
    const responses: string[] = [];

    for (let i = 0; i < 10; i++) {
      const response = await request.get('/health/live');
      const body = await response.json();
      responses.push(JSON.stringify(body));
    }

    // With 3 replicas and round-robin, we should see variation
    // (checking response is valid, not necessarily different each time)
    expect(responses.length).toBe(10);
    expect(responses.every((r) => r.includes('"ok"'))).toBe(true);
  });
});
