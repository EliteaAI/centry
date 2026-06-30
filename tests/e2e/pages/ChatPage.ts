import { Page, expect } from '@playwright/test';
import { BasePage } from './BasePage';

export class ChatPage extends BasePage {
  private readonly messageInput = '[data-testid="chat-input"], textarea[placeholder*="message"], .chat-input textarea';
  private readonly sendButton = '[data-testid="send-button"], button[aria-label="Send"]';
  private readonly messageList = '[data-testid="message-list"], .messages-container, .chat-messages';
  private readonly streamingIndicator = '[data-testid="streaming"], .streaming-indicator, .typing-indicator';

  constructor(page: Page) {
    super(page);
  }

  async navigateToChat(projectId?: string): Promise<void> {
    const path = projectId ? `/project/${projectId}/chat` : '/chat';
    await this.goto(path);
    await this.waitForAppReady();
  }

  async sendMessage(text: string): Promise<void> {
    const input = this.page.locator(this.messageInput).first();
    await input.fill(text);
    await this.page.locator(this.sendButton).first().click();
  }

  async waitForResponse(timeoutMs: number = 30_000): Promise<string> {
    // Wait for streaming to start
    await this.page.waitForSelector(this.streamingIndicator, { timeout: 10_000 }).catch(() => {});

    // Wait for streaming to finish
    await this.page.waitForSelector(this.streamingIndicator, { state: 'hidden', timeout: timeoutMs }).catch(() => {});

    // Get the last message
    const messages = this.page.locator(`${this.messageList} [data-role="assistant"]`);
    const count = await messages.count();
    if (count === 0) return '';
    return (await messages.last().textContent()) || '';
  }

  async getMessageCount(): Promise<number> {
    const messages = this.page.locator(`${this.messageList} [data-role]`);
    return messages.count();
  }

  async isStreaming(): Promise<boolean> {
    return this.hasElement(this.streamingIndicator);
  }

  async waitForSocketConnected(): Promise<void> {
    await this.page.waitForFunction(() => {
      const indicator = document.querySelector('[data-testid="connection-status"]');
      return indicator?.getAttribute('data-status') === 'connected';
    }, { timeout: 10_000 });
  }
}
