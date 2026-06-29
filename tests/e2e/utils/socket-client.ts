import { io, Socket } from 'socket.io-client';

export interface SocketMessage {
  event: string;
  data: unknown;
  timestamp: number;
}

export class SocketClient {
  private socket: Socket | null = null;
  private messages: SocketMessage[] = [];
  private baseUrl: string;

  constructor(baseUrl?: string) {
    this.baseUrl = baseUrl || process.env.BASE_URL || 'https://elitea-staging.technicaldomain.xyz';
  }

  connect(options?: {
    auth?: Record<string, string>;
    path?: string;
    reconnection?: boolean;
    reconnectionAttempts?: number;
    reconnectionDelay?: number;
    reconnectionDelayMax?: number;
  }): Promise<void> {
    return new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error('Connection timeout')), 10_000);

      this.socket = io(this.baseUrl, {
        path: options?.path || '/socket.io',
        auth: options?.auth,
        transports: ['websocket'],
        reconnection: options?.reconnection ?? true,
        reconnectionDelay: options?.reconnectionDelay ?? 1000,
        reconnectionDelayMax: options?.reconnectionDelayMax ?? 5000,
        reconnectionAttempts: options?.reconnectionAttempts ?? 10,
        rejectUnauthorized: false,
      });

      this.socket.on('connect', () => {
        clearTimeout(timeout);
        resolve();
      });

      this.socket.on('connect_error', (err) => {
        clearTimeout(timeout);
        reject(new Error(`Socket connection error: ${err.message}`));
      });

      this.socket.onAny((event, ...args) => {
        this.messages.push({ event, data: args, timestamp: Date.now() });
      });
    });
  }

  emit(event: string, data: unknown): void {
    if (!this.socket) throw new Error('Not connected');
    this.socket.emit(event, data);
  }

  waitForEvent(event: string, timeoutMs: number = 10_000): Promise<unknown> {
    return new Promise((resolve, reject) => {
      if (!this.socket) return reject(new Error('Not connected'));

      const timeout = setTimeout(
        () => reject(new Error(`Timeout waiting for event: ${event}`)),
        timeoutMs
      );

      this.socket.once(event, (data: unknown) => {
        clearTimeout(timeout);
        resolve(data);
      });
    });
  }

  getMessages(event?: string): SocketMessage[] {
    if (event) return this.messages.filter((m) => m.event === event);
    return [...this.messages];
  }

  clearMessages(): void {
    this.messages = [];
  }

  isConnected(): boolean {
    return this.socket?.connected ?? false;
  }

  disconnect(): void {
    if (this.socket) {
      this.socket.disconnect();
      this.socket = null;
    }
  }

  getSocketId(): string | undefined {
    return this.socket?.id;
  }

  onDisconnect(callback: (reason: string) => void): void {
    this.socket?.on('disconnect', callback);
  }

  onReconnect(callback: () => void): void {
    this.socket?.io.on('reconnect', callback);
  }

  onReconnectAttempt(callback: (attempt: number) => void): void {
    this.socket?.io.on('reconnect_attempt', callback);
  }

  forceDisconnect(): void {
    if (this.socket) {
      this.socket.io.engine?.close();
    }
  }
}

export function createSocketClient(baseUrl?: string): SocketClient {
  return new SocketClient(baseUrl);
}
