import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { ApiClient, createApiClient } from './api-client';

describe('ApiClient', () => {
  const mockFetch = vi.fn();

  beforeEach(() => {
    vi.stubGlobal('fetch', mockFetch);
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  describe('constructor', () => {
    it('uses provided baseUrl', () => {
      const client = new ApiClient('http://localhost:8080');
      expect(client).toBeDefined();
    });

    it('uses BASE_URL env var when no baseUrl provided', () => {
      process.env.BASE_URL = 'http://env-url';
      const client = new ApiClient();
      expect(client).toBeDefined();
      delete process.env.BASE_URL;
    });

    it('defaults to staging URL', () => {
      delete process.env.BASE_URL;
      const client = new ApiClient();
      expect(client).toBeDefined();
    });
  });

  describe('healthLive', () => {
    it('returns health data on 200', async () => {
      const healthData = { status: 'ok', checks: { redis: { status: 'ok' } } };
      mockFetch.mockResolvedValue({
        ok: true,
        status: 200,
        json: () => Promise.resolve(healthData),
      });

      const client = new ApiClient('http://test');
      const result = await client.healthLive();

      expect(result).toEqual(healthData);
      expect(mockFetch).toHaveBeenCalledWith(
        'http://test/health/live',
        expect.objectContaining({ signal: expect.any(AbortSignal) })
      );
    });

    it('returns health data on 503 (unhealthy but valid response)', async () => {
      const healthData = { status: 'unhealthy', checks: { redis: { status: 'unhealthy' } } };
      mockFetch.mockResolvedValue({
        ok: false,
        status: 503,
        json: () => Promise.resolve(healthData),
      });

      const client = new ApiClient('http://test');
      const result = await client.healthLive();
      expect(result).toEqual(healthData);
    });

    it('throws on non-503 error status', async () => {
      mockFetch.mockResolvedValue({
        ok: false,
        status: 404,
        statusText: 'Not Found',
      });

      const client = new ApiClient('http://test');
      await expect(client.healthLive()).rejects.toThrow('Health check failed: 404 Not Found');
    });
  });

  describe('healthReady', () => {
    it('returns ready data on 200', async () => {
      const readyData = { status: 'ok', checks: { plugins: { status: 'ok' } } };
      mockFetch.mockResolvedValue({
        ok: true,
        status: 200,
        json: () => Promise.resolve(readyData),
      });

      const client = new ApiClient('http://test');
      const result = await client.healthReady();

      expect(result).toEqual(readyData);
      expect(mockFetch).toHaveBeenCalledWith(
        'http://test/health/ready',
        expect.objectContaining({ signal: expect.any(AbortSignal) })
      );
    });

    it('returns data on 503', async () => {
      const data = { status: 'unhealthy', checks: {} };
      mockFetch.mockResolvedValue({
        ok: false,
        status: 503,
        json: () => Promise.resolve(data),
      });

      const client = new ApiClient('http://test');
      const result = await client.healthReady();
      expect(result).toEqual(data);
    });

    it('throws on non-503 error status', async () => {
      mockFetch.mockResolvedValue({
        ok: false,
        status: 500,
        statusText: 'Internal Server Error',
      });

      const client = new ApiClient('http://test');
      await expect(client.healthReady()).rejects.toThrow('Readiness check failed: 500');
    });
  });

  describe('waitForHealthy', () => {
    it('resolves when service becomes healthy', async () => {
      let callCount = 0;
      mockFetch.mockImplementation(() => {
        callCount++;
        if (callCount < 3) {
          return Promise.reject(new Error('Connection refused'));
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({ status: 'ok', checks: {} }),
        });
      });

      const client = new ApiClient('http://test');
      await expect(client.waitForHealthy(5, 10)).resolves.toBeUndefined();
      expect(callCount).toBe(3);
    });

    it('resolves immediately when already healthy', async () => {
      mockFetch.mockResolvedValue({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ status: 'ok', checks: {} }),
      });

      const client = new ApiClient('http://test');
      await expect(client.waitForHealthy(5, 10)).resolves.toBeUndefined();
    });

    it('throws after max attempts', async () => {
      mockFetch.mockRejectedValue(new Error('Connection refused'));

      const client = new ApiClient('http://test');
      await expect(client.waitForHealthy(3, 10)).rejects.toThrow('not healthy after 3 attempts');
    });

    it('continues when health response is degraded', async () => {
      let callCount = 0;
      mockFetch.mockImplementation(() => {
        callCount++;
        if (callCount < 3) {
          return Promise.resolve({
            ok: true,
            status: 200,
            json: () => Promise.resolve({ status: 'degraded', checks: {} }),
          });
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({ status: 'ok', checks: {} }),
        });
      });

      const client = new ApiClient('http://test');
      await expect(client.waitForHealthy(5, 10)).resolves.toBeUndefined();
      expect(callCount).toBe(3);
    });
  });

  describe('getWithAuth', () => {
    it('sends GET request with auth header', async () => {
      mockFetch.mockResolvedValue({ ok: true, status: 200 });

      const client = new ApiClient('http://test');
      await client.getWithAuth('/api/v1/me', 'my-token');

      expect(mockFetch).toHaveBeenCalledWith(
        'http://test/api/v1/me',
        expect.objectContaining({
          headers: {
            Authorization: 'Bearer my-token',
            'Content-Type': 'application/json',
          },
        })
      );
    });
  });

  describe('createApiClient', () => {
    it('creates an ApiClient instance', () => {
      const client = createApiClient('http://custom');
      expect(client).toBeInstanceOf(ApiClient);
    });
  });
});
