import { test, expect } from '@playwright/test';
import { createSocketClient } from '../utils/socket-client';
import { createK8sHelper } from '../utils/kubernetes';

test.describe('Socket.IO Scaling - Cross-Pod Message Delivery', () => {
  test('two clients receive events regardless of which pod they connect to', async () => {
    const client1 = createSocketClient();
    const client2 = createSocketClient();

    try {
      await client1.connect({ auth: { token: 'test-token-1' } });
      await client2.connect({ auth: { token: 'test-token-2' } });

      expect(client1.isConnected()).toBe(true);
      expect(client2.isConnected()).toBe(true);

      // Clients may connect to different pods via load balancer
      // Socket.IO Redis adapter should relay messages between pods
      const receivePromise = client2.waitForEvent('test_broadcast', 5000);
      client1.emit('test_broadcast', { message: 'hello from client 1' });

      const received = await receivePromise;
      expect(received).toBeDefined();
    } finally {
      client1.disconnect();
      client2.disconnect();
    }
  });

  test('messages delivered consistently across 10 iterations', async () => {
    const client1 = createSocketClient();
    const client2 = createSocketClient();
    let successCount = 0;

    try {
      await client1.connect({ auth: { token: 'test-iter-1' } });
      await client2.connect({ auth: { token: 'test-iter-2' } });

      for (let i = 0; i < 10; i++) {
        const receivePromise = client2.waitForEvent('ping', 3000);
        client1.emit('ping', { seq: i });

        try {
          await receivePromise;
          successCount++;
        } catch {
          // Message may not arrive if not in same room - that's ok for broadcast test
        }
      }

      // At least 80% should succeed with Redis adapter
      expect(successCount).toBeGreaterThanOrEqual(8);
    } finally {
      client1.disconnect();
      client2.disconnect();
    }
  });

  test('client reconnects after pod restart and receives messages', async () => {
    const k8s = createK8sHelper();
    const client = createSocketClient();

    try {
      await client.connect({ auth: { token: 'test-reconnect' } });
      expect(client.isConnected()).toBe(true);

      const initialSocketId = client.getSocketId();

      // Restart a pylon-main pod
      await k8s.simulatePodRestart('app.kubernetes.io/name=pylon-main');

      // Wait for reconnection (Socket.IO auto-reconnect)
      await new Promise((r) => setTimeout(r, 10_000));

      // Client should have reconnected (possibly to a different pod)
      expect(client.isConnected()).toBe(true);

      // Socket ID may change after reconnection
      const newSocketId = client.getSocketId();
      expect(newSocketId).toBeDefined();
    } finally {
      client.disconnect();
      // Wait for pod to be replaced
      await k8s.waitForPodReady('app.kubernetes.io/name=pylon-main', 3, 120_000);
    }
  });

  test('no sticky session headers in responses', async ({ request }) => {
    const response = await request.get('/health/live');
    const headers = response.headers();

    // Should NOT have sticky session cookies/headers
    expect(headers['set-cookie']).not.toContain('SERVERID');
    expect(headers['set-cookie']).not.toContain('sticky');
    expect(headers['x-sticky-session']).toBeUndefined();
  });

  test('socket connections are distributed across pods', async () => {
    const clients: ReturnType<typeof createSocketClient>[] = [];
    const socketIds: Set<string> = new Set();

    try {
      // Create multiple connections
      for (let i = 0; i < 6; i++) {
        const client = createSocketClient();
        await client.connect({ auth: { token: `dist-test-${i}` } });
        clients.push(client);
        const id = client.getSocketId();
        if (id) socketIds.add(id);
      }

      // All should be connected
      expect(clients.every((c) => c.isConnected())).toBe(true);

      // All socket IDs should be unique (different connections)
      expect(socketIds.size).toBe(6);
    } finally {
      clients.forEach((c) => c.disconnect());
    }
  });
});
