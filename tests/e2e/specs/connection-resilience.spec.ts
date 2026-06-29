import { test, expect } from '@playwright/test';
import { ChatPage } from '../pages/ChatPage';
import { LoginPage } from '../pages/LoginPage';
import { createSocketClient } from '../utils/socket-client';
import { createK8sHelper } from '../utils/kubernetes';

test.describe('Connection Resilience - Auto-Reconnect & Recovery', () => {
  test('Socket.IO client reconnects after network interruption', async ({ page }) => {
    const loginPage = new LoginPage(page);
    const chatPage = new ChatPage(page);

    await loginPage.completeOidcLogin('testuser', 'testpass');
    await chatPage.waitForSocketConnected();

    // Simulate network interruption by blocking WebSocket traffic
    await page.route('**/socket.io/**', (route) => route.abort());

    // Wait for disconnect detection (Socket.IO pingTimeout default: 20s)
    await page.waitForTimeout(5000);

    // Unblock traffic
    await page.unroute('**/socket.io/**');

    // Socket.IO should auto-reconnect within its reconnection delay
    await page.waitForTimeout(10_000);

    // Verify reconnection by checking socket state
    const isConnected = await page.evaluate(() => {
      const socket = (window as any).__socketIO;
      return socket?.connected ?? false;
    });

    expect(isConnected).toBe(true);
  });

  test('reconnects within 5 seconds after brief interruption', async ({ page }) => {
    const loginPage = new LoginPage(page);
    const chatPage = new ChatPage(page);

    await loginPage.completeOidcLogin('testuser', 'testpass');
    await chatPage.waitForSocketConnected();

    // Brief interruption (1 second block)
    await page.route('**/socket.io/**', (route) => route.abort());
    await page.waitForTimeout(1000);
    await page.unroute('**/socket.io/**');

    // Should reconnect within 5 seconds
    const reconnected = await page.waitForFunction(
      () => (window as any).__socketIO?.connected === true,
      { timeout: 5000 }
    ).then(() => true).catch(() => false);

    expect(reconnected).toBe(true);
  });

  test('raw Socket.IO client auto-reconnects after pod restart', async () => {
    const k8s = createK8sHelper();
    const client = createSocketClient();

    await client.connect({
      auth: { token: 'resilience-test' },
      reconnection: true,
      reconnectionAttempts: 10,
      reconnectionDelay: 1000,
    });

    expect(client.isConnected()).toBe(true);

    // Track disconnection/reconnection events
    let disconnected = false;
    let reconnected = false;

    client.onDisconnect(() => { disconnected = true; });
    client.onReconnect(() => { reconnected = true; });

    // Restart a pylon-main pod
    await k8s.simulatePodRestart('app.kubernetes.io/name=pylon-main');

    // Wait for reconnection cycle
    await new Promise((r) => setTimeout(r, 15_000));

    // Client should have detected disconnect and reconnected
    expect(client.isConnected()).toBe(true);

    client.disconnect();
    await k8s.waitForPodReady('app.kubernetes.io/name=pylon-main', 3, 120_000);
  });

  test('chat message can be retried after reconnection', async ({ page }) => {
    const loginPage = new LoginPage(page);
    const chatPage = new ChatPage(page);

    await loginPage.completeOidcLogin('testuser', 'testpass');
    await chatPage.waitForSocketConnected();

    // Send a message while connected
    await chatPage.sendMessage('Hello before disconnect');
    const firstResponse = await chatPage.waitForResponse(30_000);
    expect(firstResponse).toBeTruthy();

    // Simulate brief disconnect
    await page.route('**/socket.io/**', (route) => route.abort());
    await page.waitForTimeout(2000);
    await page.unroute('**/socket.io/**');

    // Wait for reconnection
    await page.waitForFunction(
      () => (window as any).__socketIO?.connected === true,
      { timeout: 10_000 }
    );

    // Send another message after reconnection
    await chatPage.sendMessage('Hello after reconnect');
    const secondResponse = await chatPage.waitForResponse(30_000);
    expect(secondResponse).toBeTruthy();
  });

  test('multiple reconnection attempts with exponential backoff', async () => {
    const client = createSocketClient();
    const reconnectAttempts: number[] = [];

    await client.connect({
      auth: { token: 'backoff-test' },
      reconnection: true,
      reconnectionAttempts: 5,
      reconnectionDelay: 500,
      reconnectionDelayMax: 5000,
    });

    expect(client.isConnected()).toBe(true);

    // Force disconnect (simulate server going away entirely)
    client.onReconnectAttempt((attempt: number) => {
      reconnectAttempts.push(Date.now());
    });

    // Disconnect without server-side cleanup to trigger reconnection
    client.forceDisconnect();

    // Wait for a few reconnection attempts
    await new Promise((r) => setTimeout(r, 8000));

    // Should have made multiple attempts
    expect(reconnectAttempts.length).toBeGreaterThan(1);

    // Verify backoff: later attempts should have longer intervals
    if (reconnectAttempts.length >= 3) {
      const interval1 = reconnectAttempts[1] - reconnectAttempts[0];
      const interval2 = reconnectAttempts[2] - reconnectAttempts[1];
      // Second interval should be >= first (exponential backoff)
      expect(interval2).toBeGreaterThanOrEqual(interval1 * 0.8); // 0.8 to account for jitter
    }

    client.disconnect();
  });
});
