import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const mockSocket = {
  on: vi.fn(),
  once: vi.fn(),
  onAny: vi.fn(),
  emit: vi.fn(),
  disconnect: vi.fn(),
  connected: true,
  id: 'mock-socket-id',
  io: {
    on: vi.fn(),
    engine: { close: vi.fn() },
  },
};

vi.mock('socket.io-client', () => ({
  io: vi.fn(() => mockSocket),
}));

import { SocketClient, createSocketClient } from './socket-client';

describe('SocketClient', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockSocket.connected = true;
    mockSocket.id = 'mock-socket-id';
  });

  describe('constructor', () => {
    it('uses provided baseUrl', () => {
      const client = new SocketClient('http://localhost:8080');
      expect(client).toBeDefined();
    });

    it('uses BASE_URL env var', () => {
      process.env.BASE_URL = 'http://env-url';
      const client = new SocketClient();
      expect(client).toBeDefined();
      delete process.env.BASE_URL;
    });

    it('defaults to staging URL', () => {
      delete process.env.BASE_URL;
      const client = new SocketClient();
      expect(client).toBeDefined();
    });
  });

  describe('connect', () => {
    it('resolves on successful connection', async () => {
      mockSocket.on.mockImplementation((event: string, cb: Function) => {
        if (event === 'connect') setTimeout(() => cb(), 0);
      });

      const client = new SocketClient('http://test');
      await expect(client.connect()).resolves.toBeUndefined();
    });

    it('rejects on connection error', async () => {
      mockSocket.on.mockImplementation((event: string, cb: Function) => {
        if (event === 'connect_error') setTimeout(() => cb(new Error('refused')), 0);
      });

      const client = new SocketClient('http://test');
      await expect(client.connect()).rejects.toThrow('Socket connection error: refused');
    });

    it('passes auth and reconnection options', async () => {
      const { io } = await import('socket.io-client');
      mockSocket.on.mockImplementation((event: string, cb: Function) => {
        if (event === 'connect') setTimeout(() => cb(), 0);
      });

      const client = new SocketClient('http://test');
      await client.connect({
        auth: { token: 'abc' },
        path: '/custom',
        reconnection: false,
        reconnectionAttempts: 5,
        reconnectionDelay: 2000,
        reconnectionDelayMax: 10000,
      });

      expect(io).toHaveBeenCalledWith('http://test', expect.objectContaining({
        path: '/custom',
        auth: { token: 'abc' },
        reconnection: false,
        reconnectionAttempts: 5,
        reconnectionDelay: 2000,
        reconnectionDelayMax: 10000,
      }));
    });

    it('registers onAny handler for message recording', async () => {
      mockSocket.on.mockImplementation((event: string, cb: Function) => {
        if (event === 'connect') setTimeout(() => cb(), 0);
      });

      const client = new SocketClient('http://test');
      await client.connect();

      expect(mockSocket.onAny).toHaveBeenCalledWith(expect.any(Function));
    });
  });

  describe('emit', () => {
    it('emits event on connected socket', async () => {
      mockSocket.on.mockImplementation((event: string, cb: Function) => {
        if (event === 'connect') setTimeout(() => cb(), 0);
      });

      const client = new SocketClient('http://test');
      await client.connect();
      client.emit('test_event', { data: 'hello' });

      expect(mockSocket.emit).toHaveBeenCalledWith('test_event', { data: 'hello' });
    });

    it('throws when not connected', () => {
      const client = new SocketClient('http://test');
      expect(() => client.emit('test', {})).toThrow('Not connected');
    });
  });

  describe('waitForEvent', () => {
    it('resolves when event received', async () => {
      mockSocket.on.mockImplementation((event: string, cb: Function) => {
        if (event === 'connect') setTimeout(() => cb(), 0);
      });
      mockSocket.once.mockImplementation((event: string, cb: Function) => {
        if (event === 'test_event') setTimeout(() => cb({ result: 'ok' }), 10);
      });

      const client = new SocketClient('http://test');
      await client.connect();
      const result = await client.waitForEvent('test_event', 5000);
      expect(result).toEqual({ result: 'ok' });
    });

    it('rejects when not connected', async () => {
      const client = new SocketClient('http://test');
      await expect(client.waitForEvent('any')).rejects.toThrow('Not connected');
    });

    it('rejects on timeout', async () => {
      mockSocket.on.mockImplementation((event: string, cb: Function) => {
        if (event === 'connect') setTimeout(() => cb(), 0);
      });
      mockSocket.once.mockImplementation(() => {});

      const client = new SocketClient('http://test');
      await client.connect();
      await expect(client.waitForEvent('never', 50)).rejects.toThrow('Timeout waiting for event: never');
    });
  });

  describe('getMessages', () => {
    it('returns all messages when no filter', async () => {
      let onAnyHandler: Function = () => {};
      mockSocket.on.mockImplementation((event: string, cb: Function) => {
        if (event === 'connect') setTimeout(() => cb(), 0);
      });
      mockSocket.onAny.mockImplementation((handler: Function) => {
        onAnyHandler = handler;
      });

      const client = new SocketClient('http://test');
      await client.connect();

      onAnyHandler('event1', 'data1');
      onAnyHandler('event2', 'data2');

      const messages = client.getMessages();
      expect(messages).toHaveLength(2);
      expect(messages[0].event).toBe('event1');
      expect(messages[1].event).toBe('event2');
    });

    it('filters messages by event name', async () => {
      let onAnyHandler: Function = () => {};
      mockSocket.on.mockImplementation((event: string, cb: Function) => {
        if (event === 'connect') setTimeout(() => cb(), 0);
      });
      mockSocket.onAny.mockImplementation((handler: Function) => {
        onAnyHandler = handler;
      });

      const client = new SocketClient('http://test');
      await client.connect();

      onAnyHandler('event1', 'data1');
      onAnyHandler('event2', 'data2');
      onAnyHandler('event1', 'data3');

      const messages = client.getMessages('event1');
      expect(messages).toHaveLength(2);
    });
  });

  describe('clearMessages', () => {
    it('empties the message store', async () => {
      let onAnyHandler: Function = () => {};
      mockSocket.on.mockImplementation((event: string, cb: Function) => {
        if (event === 'connect') setTimeout(() => cb(), 0);
      });
      mockSocket.onAny.mockImplementation((handler: Function) => {
        onAnyHandler = handler;
      });

      const client = new SocketClient('http://test');
      await client.connect();
      onAnyHandler('ev', 'data');

      expect(client.getMessages()).toHaveLength(1);
      client.clearMessages();
      expect(client.getMessages()).toHaveLength(0);
    });
  });

  describe('isConnected', () => {
    it('returns true when socket is connected', async () => {
      mockSocket.on.mockImplementation((event: string, cb: Function) => {
        if (event === 'connect') setTimeout(() => cb(), 0);
      });

      const client = new SocketClient('http://test');
      await client.connect();
      expect(client.isConnected()).toBe(true);
    });

    it('returns false when no socket', () => {
      const client = new SocketClient('http://test');
      expect(client.isConnected()).toBe(false);
    });
  });

  describe('disconnect', () => {
    it('disconnects and nullifies socket', async () => {
      mockSocket.on.mockImplementation((event: string, cb: Function) => {
        if (event === 'connect') setTimeout(() => cb(), 0);
      });

      const client = new SocketClient('http://test');
      await client.connect();
      client.disconnect();

      expect(mockSocket.disconnect).toHaveBeenCalled();
      expect(client.isConnected()).toBe(false);
    });

    it('does nothing when not connected', () => {
      const client = new SocketClient('http://test');
      client.disconnect(); // should not throw
    });
  });

  describe('getSocketId', () => {
    it('returns socket id when connected', async () => {
      mockSocket.on.mockImplementation((event: string, cb: Function) => {
        if (event === 'connect') setTimeout(() => cb(), 0);
      });

      const client = new SocketClient('http://test');
      await client.connect();
      expect(client.getSocketId()).toBe('mock-socket-id');
    });

    it('returns undefined when not connected', () => {
      const client = new SocketClient('http://test');
      expect(client.getSocketId()).toBeUndefined();
    });
  });

  describe('onDisconnect', () => {
    it('registers disconnect callback', async () => {
      mockSocket.on.mockImplementation((event: string, cb: Function) => {
        if (event === 'connect') setTimeout(() => cb(), 0);
      });

      const client = new SocketClient('http://test');
      await client.connect();

      const handler = vi.fn();
      client.onDisconnect(handler);

      expect(mockSocket.on).toHaveBeenCalledWith('disconnect', handler);
    });
  });

  describe('onReconnect', () => {
    it('registers reconnect callback on io manager', async () => {
      mockSocket.on.mockImplementation((event: string, cb: Function) => {
        if (event === 'connect') setTimeout(() => cb(), 0);
      });

      const client = new SocketClient('http://test');
      await client.connect();

      const handler = vi.fn();
      client.onReconnect(handler);

      expect(mockSocket.io.on).toHaveBeenCalledWith('reconnect', handler);
    });
  });

  describe('onReconnectAttempt', () => {
    it('registers reconnect_attempt callback', async () => {
      mockSocket.on.mockImplementation((event: string, cb: Function) => {
        if (event === 'connect') setTimeout(() => cb(), 0);
      });

      const client = new SocketClient('http://test');
      await client.connect();

      const handler = vi.fn();
      client.onReconnectAttempt(handler);

      expect(mockSocket.io.on).toHaveBeenCalledWith('reconnect_attempt', handler);
    });
  });

  describe('forceDisconnect', () => {
    it('closes the engine transport', async () => {
      mockSocket.on.mockImplementation((event: string, cb: Function) => {
        if (event === 'connect') setTimeout(() => cb(), 0);
      });

      const client = new SocketClient('http://test');
      await client.connect();
      client.forceDisconnect();

      expect(mockSocket.io.engine.close).toHaveBeenCalled();
    });
  });

  describe('createSocketClient', () => {
    it('creates a SocketClient instance', () => {
      const client = createSocketClient('http://custom');
      expect(client).toBeInstanceOf(SocketClient);
    });
  });
});
