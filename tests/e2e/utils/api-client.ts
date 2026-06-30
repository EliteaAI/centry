export interface HealthCheckResult {
  status: 'ok' | 'degraded' | 'unhealthy';
  checks: Record<string, { status: string; latency_ms?: number; error?: string }>;
}

export class ApiClient {
  private baseUrl: string;
  private timeout: number;

  constructor(baseUrl?: string, timeout?: number) {
    this.baseUrl = baseUrl || process.env.BASE_URL || 'https://elitea-staging.technicaldomain.xyz';
    this.timeout = timeout || 10_000;
  }

  async healthLive(): Promise<HealthCheckResult> {
    const response = await fetch(`${this.baseUrl}/health/live`, {
      signal: AbortSignal.timeout(this.timeout),
    });
    if (!response.ok && response.status !== 503) {
      throw new Error(`Health check failed: ${response.status} ${response.statusText}`);
    }
    return response.json() as Promise<HealthCheckResult>;
  }

  async healthReady(): Promise<HealthCheckResult> {
    const response = await fetch(`${this.baseUrl}/health/ready`, {
      signal: AbortSignal.timeout(this.timeout),
    });
    if (!response.ok && response.status !== 503) {
      throw new Error(`Readiness check failed: ${response.status} ${response.statusText}`);
    }
    return response.json() as Promise<HealthCheckResult>;
  }

  async waitForHealthy(maxAttempts: number = 30, delayMs: number = 2000): Promise<void> {
    for (let i = 0; i < maxAttempts; i++) {
      try {
        const result = await this.healthLive();
        if (result.status === 'ok') return;
      } catch {
        // Continue waiting
      }
      await new Promise((r) => setTimeout(r, delayMs));
    }
    throw new Error(`Service not healthy after ${maxAttempts} attempts`);
  }

  async getWithAuth(path: string, token: string): Promise<Response> {
    return fetch(`${this.baseUrl}${path}`, {
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
      signal: AbortSignal.timeout(this.timeout),
    });
  }
}

export function createApiClient(baseUrl?: string): ApiClient {
  return new ApiClient(baseUrl);
}
