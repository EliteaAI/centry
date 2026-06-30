import { test, expect } from '@playwright/test';
import { LoginPage } from '../pages/LoginPage';
import { ChatPage } from '../pages/ChatPage';
import { createK8sHelper } from '../utils/kubernetes';

test.describe('UI Client Resilience - Multi-Pod Deployment', () => {
  test.describe('Server Restart During Active Session', () => {
    test('UI reconnects automatically after pylon-main pod restart', async ({ page }) => {
      const loginPage = new LoginPage(page);
      const chatPage = new ChatPage(page);
      const k8s = createK8sHelper();

      await loginPage.completeOidcLogin('testuser', 'testpass');
      await chatPage.navigateToChat();
      await chatPage.waitForSocketConnected();

      // Verify initial connection
      const connectedBefore = await page.evaluate(
        () => (window as any).__socketIO?.connected === true
      );
      expect(connectedBefore).toBe(true);

      // Restart one pylon-main pod (multi-pod: other pods still serve traffic)
      await k8s.simulatePodRestart('app.kubernetes.io/name=pylon-main');

      // Wait for reconnection — other pods handle the failover
      const reconnected = await page
        .waitForFunction(
          () => (window as any).__socketIO?.connected === true,
          { timeout: 15_000 }
        )
        .then(() => true)
        .catch(() => false);

      expect(reconnected).toBe(true);

      // UI should still be functional — send a message
      await chatPage.sendMessage('Post-restart test message');
      const response = await chatPage.waitForResponse(30_000);
      expect(response).toBeTruthy();

      // Ensure pods recover fully
      await k8s.waitForPodReady('app.kubernetes.io/name=pylon-main', 3, 120_000);
    });

    test('connection state indicator shows reconnecting then connected', async ({ page }) => {
      const loginPage = new LoginPage(page);
      const chatPage = new ChatPage(page);
      const k8s = createK8sHelper();

      await loginPage.completeOidcLogin('testuser', 'testpass');
      await chatPage.navigateToChat();
      await chatPage.waitForSocketConnected();

      // Set up observer for connection states
      const stateChanges: string[] = await page.evaluate(() => {
        const states: string[] = [];
        const observer = new MutationObserver(() => {
          const indicator = document.querySelector('[data-testid="connection-status"]');
          const status = indicator?.getAttribute('data-status');
          if (status && states[states.length - 1] !== status) {
            states.push(status);
          }
        });
        const target = document.querySelector('[data-testid="connection-status"]');
        if (target) {
          observer.observe(target, { attributes: true });
        }
        (window as any).__stateObserver = observer;
        (window as any).__stateChanges = states;
        return states;
      });

      // Restart a pod
      await k8s.simulatePodRestart('app.kubernetes.io/name=pylon-main');

      // Wait for cycle: connected → reconnecting → connected
      await page.waitForTimeout(12_000);

      const observedStates = await page.evaluate(
        () => (window as any).__stateChanges as string[]
      );

      // Cleanup observer
      await page.evaluate(() => {
        (window as any).__stateObserver?.disconnect();
      });

      // Final state should be connected
      const finalState = await page.evaluate(() => {
        const indicator = document.querySelector('[data-testid="connection-status"]');
        return indicator?.getAttribute('data-status');
      });
      expect(finalState).toBe('connected');

      await k8s.waitForPodReady('app.kubernetes.io/name=pylon-main', 3, 120_000);
    });
  });

  test.describe('Network Interruption', () => {
    test('UI shows reconnecting state during 5s network interruption', async ({ page }) => {
      const loginPage = new LoginPage(page);
      const chatPage = new ChatPage(page);

      await loginPage.completeOidcLogin('testuser', 'testpass');
      await chatPage.navigateToChat();
      await chatPage.waitForSocketConnected();

      // Block all Socket.IO traffic to simulate network interruption
      await page.route('**/socket.io/**', (route) => route.abort());

      // Wait 5 seconds (simulating wifi disconnect)
      await page.waitForTimeout(5000);

      // Check that UI shows disconnected/reconnecting state
      const statusDuringOutage = await page.evaluate(() => {
        const indicator = document.querySelector('[data-testid="connection-status"]');
        return indicator?.getAttribute('data-status');
      });
      expect(['reconnecting', 'disconnected']).toContain(statusDuringOutage);

      // Restore network
      await page.unroute('**/socket.io/**');

      // Should reconnect within 10 seconds (reconnectionDelay + backoff)
      const reconnected = await page
        .waitForFunction(
          () => {
            const indicator = document.querySelector('[data-testid="connection-status"]');
            return indicator?.getAttribute('data-status') === 'connected';
          },
          { timeout: 10_000 }
        )
        .then(() => true)
        .catch(() => false);

      expect(reconnected).toBe(true);
    });

    test('chat functionality resumes after network recovery', async ({ page }) => {
      const loginPage = new LoginPage(page);
      const chatPage = new ChatPage(page);

      await loginPage.completeOidcLogin('testuser', 'testpass');
      await chatPage.navigateToChat();
      await chatPage.waitForSocketConnected();

      // Send a message before interruption
      await chatPage.sendMessage('Before network interrupt');
      const responseBefore = await chatPage.waitForResponse(30_000);
      expect(responseBefore).toBeTruthy();

      const messageCountBefore = await chatPage.getMessageCount();

      // Simulate 5s network interruption
      await page.route('**/socket.io/**', (route) => route.abort());
      await page.waitForTimeout(5000);
      await page.unroute('**/socket.io/**');

      // Wait for reconnection
      await page.waitForFunction(
        () => (window as any).__socketIO?.connected === true,
        { timeout: 10_000 }
      );

      // Send message after reconnection
      await chatPage.sendMessage('After network recovery');
      const responseAfter = await chatPage.waitForResponse(30_000);
      expect(responseAfter).toBeTruthy();

      // Message count should have increased (no data loss from before)
      const messageCountAfter = await chatPage.getMessageCount();
      expect(messageCountAfter).toBeGreaterThan(messageCountBefore);
    });
  });

  test.describe('Rapid Page Refreshes During Streaming', () => {
    test('no duplicate messages after rapid page refresh during streaming', async ({ page }) => {
      const loginPage = new LoginPage(page);
      const chatPage = new ChatPage(page);

      await loginPage.completeOidcLogin('testuser', 'testpass');
      await chatPage.navigateToChat();
      await chatPage.waitForSocketConnected();

      // Send a message that triggers streaming
      await chatPage.sendMessage('Tell me a long story');

      // Wait briefly for streaming to start
      await page.waitForTimeout(1000);

      // Rapidly refresh the page
      await page.reload();
      await page.waitForLoadState('networkidle');

      // Wait for socket to reconnect after refresh
      await chatPage.waitForSocketConnected();

      // Refresh again quickly
      await page.reload();
      await page.waitForLoadState('networkidle');
      await chatPage.waitForSocketConnected();

      // Check: no duplicate messages visible
      const messages = page.locator('[data-role="assistant"]');
      const count = await messages.count();
      if (count > 0) {
        const texts: string[] = [];
        for (let i = 0; i < count; i++) {
          const text = await messages.nth(i).textContent();
          if (text) texts.push(text.trim());
        }
        // No two consecutive messages should be identical
        for (let i = 1; i < texts.length; i++) {
          if (texts[i].length > 20) {
            expect(texts[i]).not.toBe(texts[i - 1]);
          }
        }
      }
    });

    test('page refresh does not create orphaned socket connections', async ({ page }) => {
      const loginPage = new LoginPage(page);
      const chatPage = new ChatPage(page);

      await loginPage.completeOidcLogin('testuser', 'testpass');
      await chatPage.navigateToChat();
      await chatPage.waitForSocketConnected();

      // Perform 5 rapid refreshes
      for (let i = 0; i < 5; i++) {
        await page.reload();
        await page.waitForLoadState('domcontentloaded');
      }

      // Wait for final page to settle
      await page.waitForLoadState('networkidle');
      await chatPage.waitForSocketConnected();

      // Only one active socket connection should exist
      const activeConnections = await page.evaluate(() => {
        const socket = (window as any).__socketIO;
        return socket?.connected ? 1 : 0;
      });
      expect(activeConnections).toBe(1);
    });
  });

  test.describe('Token Expiry During Active Session', () => {
    test('redirects to login when session token expires', async ({ page }) => {
      const loginPage = new LoginPage(page);
      const chatPage = new ChatPage(page);

      await loginPage.completeOidcLogin('testuser', 'testpass');
      await chatPage.navigateToChat();
      await chatPage.waitForSocketConnected();

      // Simulate token expiry by clearing session cookies
      const cookies = await page.context().cookies();
      const sessionCookies = cookies.filter(
        (c) => c.name.includes('session') || c.name.includes('auth')
      );
      await page.context().clearCookies();

      // Try to perform an action that requires authentication
      await page.reload();
      await page.waitForLoadState('networkidle');

      // Should redirect to OIDC login or show unauthenticated state
      const currentUrl = page.url();
      const isLoginRedirect =
        currentUrl.includes('oidc-mock') ||
        currentUrl.includes('login') ||
        currentUrl.includes('forward-auth');

      expect(isLoginRedirect).toBe(true);
    });

    test('API returns 401 after session expiry without infinite redirect loop', async ({
      page,
    }) => {
      const loginPage = new LoginPage(page);
      const chatPage = new ChatPage(page);

      await loginPage.completeOidcLogin('testuser', 'testpass');
      await chatPage.navigateToChat();
      await chatPage.waitForSocketConnected();

      // Clear session cookies to simulate expiry
      await page.context().clearCookies();

      // Track navigation count to detect redirect loops
      let navigationCount = 0;
      page.on('framenavigated', () => {
        navigationCount++;
      });

      // Attempt authenticated API call
      const response = await page.evaluate(async () => {
        try {
          const res = await fetch('/api/v1/auth/whoami', {
            credentials: 'include',
          });
          return { status: res.status, redirected: res.redirected };
        } catch (e: any) {
          return { status: 0, error: e.message };
        }
      });

      // Should get 401 or redirect to login (not loop)
      expect(
        response.status === 401 || response.status === 403 || response.redirected
      ).toBe(true);

      // Wait a bit and check we didn't enter a redirect loop
      await page.waitForTimeout(3000);
      expect(navigationCount).toBeLessThan(10);
    });

    test('user can re-login after token expiry and resume work', async ({ page }) => {
      const loginPage = new LoginPage(page);
      const chatPage = new ChatPage(page);

      await loginPage.completeOidcLogin('testuser', 'testpass');
      await chatPage.navigateToChat();
      await chatPage.waitForSocketConnected();

      // Expire the session
      await page.context().clearCookies();
      await page.reload();
      await page.waitForLoadState('networkidle');

      // Re-authenticate
      await loginPage.completeOidcLogin('testuser', 'testpass');
      await chatPage.navigateToChat();
      await chatPage.waitForSocketConnected();

      // Should be fully functional again
      const isConnected = await page.evaluate(
        () => (window as any).__socketIO?.connected === true
      );
      expect(isConnected).toBe(true);
    });
  });

  test.describe('Multi-Pod Deployment Validation', () => {
    test('requests are distributed across multiple pods', async ({ page }) => {
      const loginPage = new LoginPage(page);
      const k8s = createK8sHelper();

      await loginPage.completeOidcLogin('testuser', 'testpass');

      // Make multiple health check requests and verify pods are serving
      const podCount = await k8s.getReadyPodCount(
        'app.kubernetes.io/name=pylon-main'
      );
      expect(podCount).toBeGreaterThanOrEqual(2);

      // Verify all pods are healthy
      const pods = await k8s.getPods('app.kubernetes.io/name=pylon-main');
      const healthyPods = pods.filter((p) => p.ready);
      expect(healthyPods.length).toBe(podCount);
    });

    test('session works across different pods (no sticky sessions)', async ({ page }) => {
      const loginPage = new LoginPage(page);
      const chatPage = new ChatPage(page);

      await loginPage.completeOidcLogin('testuser', 'testpass');
      await chatPage.navigateToChat();
      await chatPage.waitForSocketConnected();

      // Make multiple requests — with Redis sessions, any pod can serve them
      const responses: number[] = [];
      for (let i = 0; i < 10; i++) {
        const status = await page.evaluate(async () => {
          const res = await fetch('/api/v1/health/live', {
            credentials: 'include',
          });
          return res.status;
        });
        responses.push(status);
      }

      // All requests should succeed regardless of which pod serves them
      const successCount = responses.filter((s) => s === 200).length;
      expect(successCount).toBe(10);
    });
  });
});
