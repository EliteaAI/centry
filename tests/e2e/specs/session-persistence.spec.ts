import { test, expect } from '@playwright/test';
import { LoginPage } from '../pages/LoginPage';
import { createK8sHelper } from '../utils/kubernetes';
import { createApiClient } from '../utils/api-client';

test.describe('Session Persistence - Pod Restart Resilience', () => {
  test('login session survives pylon-main pod restart', async ({ page }) => {
    const k8s = createK8sHelper();
    const loginPage = new LoginPage(page);

    // Login
    await loginPage.completeOidcLogin('testuser', 'testpass');
    await loginPage.verifyLoggedIn();

    // Get current cookies
    const cookiesBefore = await page.context().cookies();
    const sessionBefore = cookiesBefore.find((c) => c.name.includes('session'));
    expect(sessionBefore).toBeDefined();

    // Restart a pylon-main pod
    const deletedPod = await k8s.simulatePodRestart('app.kubernetes.io/name=pylon-main');

    // Wait for pod replacement
    await k8s.waitForPodReady('app.kubernetes.io/name=pylon-main', 3, 120_000);

    // Refresh the page — session should still be valid
    await page.reload();
    await page.waitForLoadState('networkidle');

    // Should NOT be redirected to login
    const url = page.url();
    expect(url).not.toContain('oidc-mock');
    expect(url).not.toContain('login');

    // Should still be authenticated
    await loginPage.verifyLoggedIn();
  });

  test('login session survives pylon-auth pod restart', async ({ page }) => {
    const k8s = createK8sHelper();
    const loginPage = new LoginPage(page);

    await loginPage.completeOidcLogin('testuser', 'testpass');
    await loginPage.verifyLoggedIn();

    // Restart a pylon-auth pod
    await k8s.simulatePodRestart('app.kubernetes.io/name=pylon-auth');
    await k8s.waitForPodReady('app.kubernetes.io/name=pylon-auth', 2, 90_000);

    // Navigate to a page that requires auth
    await page.reload();
    await page.waitForLoadState('networkidle');

    // Session stored in Redis should persist across auth pod restart
    const url = page.url();
    expect(url).not.toContain('login');
  });

  test('API requests work during rolling restart', async ({ request }) => {
    const k8s = createK8sHelper();
    const apiClient = createApiClient();

    // Start continuous health checks
    const results: boolean[] = [];
    const checkInterval = setInterval(async () => {
      try {
        const result = await apiClient.healthLive();
        results.push(result.status === 'ok');
      } catch {
        results.push(false);
      }
    }, 1000);

    // Restart one pod
    await k8s.simulatePodRestart('app.kubernetes.io/name=pylon-main');

    // Continue checking for 30 seconds
    await new Promise((r) => setTimeout(r, 30_000));
    clearInterval(checkInterval);

    // With 3 replicas and one restarting, the majority of requests should succeed
    const successRate = results.filter(Boolean).length / results.length;
    expect(successRate).toBeGreaterThan(0.8);

    // Wait for full recovery
    await k8s.waitForPodReady('app.kubernetes.io/name=pylon-main', 3, 120_000);
  });

  test('concurrent users maintain independent sessions', async ({ browser }) => {
    const context1 = await browser.newContext({ ignoreHTTPSErrors: true });
    const context2 = await browser.newContext({ ignoreHTTPSErrors: true });
    const page1 = await context1.newPage();
    const page2 = await context2.newPage();

    const login1 = new LoginPage(page1);
    const login2 = new LoginPage(page2);

    // Both users login
    await login1.completeOidcLogin('user1', 'pass1');
    await login2.completeOidcLogin('user2', 'pass2');

    // Both should be authenticated independently
    await login1.verifyLoggedIn();
    await login2.verifyLoggedIn();

    // Logging out user1 should not affect user2
    await page1.goto('/forward-auth/logout');
    await page1.waitForLoadState('networkidle');

    // User2 should still be authenticated
    await page2.reload();
    await page2.waitForLoadState('networkidle');
    const url2 = page2.url();
    expect(url2).not.toContain('login');

    await context1.close();
    await context2.close();
  });
});
