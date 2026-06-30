import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('@playwright/test', () => ({
  Page: vi.fn(),
  expect: vi.fn().mockReturnValue({ toBe: vi.fn() }),
}));

function createMockPage() {
  const mockLocator = {
    first: vi.fn().mockReturnThis(),
    last: vi.fn().mockReturnThis(),
    fill: vi.fn().mockResolvedValue(undefined),
    click: vi.fn().mockResolvedValue(undefined),
    count: vi.fn().mockResolvedValue(0),
    textContent: vi.fn().mockResolvedValue(null),
  };

  return {
    goto: vi.fn().mockResolvedValue(undefined),
    waitForLoadState: vi.fn().mockResolvedValue(undefined),
    waitForURL: vi.fn().mockResolvedValue(undefined),
    waitForSelector: vi.fn().mockResolvedValue(undefined),
    waitForFunction: vi.fn().mockResolvedValue(undefined),
    title: vi.fn().mockResolvedValue('Elitea'),
    locator: vi.fn().mockReturnValue(mockLocator),
    context: vi.fn().mockReturnValue({
      cookies: vi.fn().mockResolvedValue([]),
    }),
    __mockLocator: mockLocator,
  };
}

import { ChatPage } from './ChatPage';

describe('ChatPage', () => {
  let page: ReturnType<typeof createMockPage>;

  beforeEach(() => {
    page = createMockPage();
    vi.clearAllMocks();
  });

  describe('navigateToChat', () => {
    it('navigates to /chat when no projectId', async () => {
      const chatPage = new ChatPage(page as any);
      await chatPage.navigateToChat();

      expect(page.goto).toHaveBeenCalledWith('/chat');
      expect(page.waitForLoadState).toHaveBeenCalledWith('networkidle');
    });

    it('navigates to project-specific chat', async () => {
      const chatPage = new ChatPage(page as any);
      await chatPage.navigateToChat('42');

      expect(page.goto).toHaveBeenCalledWith('/project/42/chat');
      expect(page.waitForLoadState).toHaveBeenCalledWith('networkidle');
    });
  });

  describe('sendMessage', () => {
    it('fills input and clicks send', async () => {
      const chatPage = new ChatPage(page as any);
      await chatPage.sendMessage('Hello AI');

      expect(page.locator).toHaveBeenCalled();
      expect(page.__mockLocator.fill).toHaveBeenCalledWith('Hello AI');
      expect(page.__mockLocator.click).toHaveBeenCalled();
    });
  });

  describe('waitForResponse', () => {
    it('returns last assistant message text', async () => {
      page.__mockLocator.count.mockResolvedValue(2);
      page.__mockLocator.textContent.mockResolvedValue('AI response');

      const chatPage = new ChatPage(page as any);
      const response = await chatPage.waitForResponse();

      expect(response).toBe('AI response');
    });

    it('returns empty string when no messages', async () => {
      page.__mockLocator.count.mockResolvedValue(0);

      const chatPage = new ChatPage(page as any);
      const response = await chatPage.waitForResponse();

      expect(response).toBe('');
    });

    it('handles null textContent', async () => {
      page.__mockLocator.count.mockResolvedValue(1);
      page.__mockLocator.textContent.mockResolvedValue(null);

      const chatPage = new ChatPage(page as any);
      const response = await chatPage.waitForResponse();

      expect(response).toBe('');
    });

    it('handles streaming indicator not appearing', async () => {
      page.waitForSelector.mockRejectedValue(new Error('timeout'));
      page.__mockLocator.count.mockResolvedValue(1);
      page.__mockLocator.textContent.mockResolvedValue('response');

      const chatPage = new ChatPage(page as any);
      const response = await chatPage.waitForResponse(5000);

      expect(response).toBe('response');
    });
  });

  describe('getMessageCount', () => {
    it('returns count of message elements', async () => {
      page.__mockLocator.count.mockResolvedValue(5);

      const chatPage = new ChatPage(page as any);
      const count = await chatPage.getMessageCount();

      expect(count).toBe(5);
    });

    it('returns 0 when no messages', async () => {
      page.__mockLocator.count.mockResolvedValue(0);

      const chatPage = new ChatPage(page as any);
      const count = await chatPage.getMessageCount();

      expect(count).toBe(0);
    });
  });

  describe('isStreaming', () => {
    it('returns true when streaming indicator present', async () => {
      page.__mockLocator.count.mockResolvedValue(1);

      const chatPage = new ChatPage(page as any);
      const result = await chatPage.isStreaming();

      expect(result).toBe(true);
    });

    it('returns false when streaming indicator absent', async () => {
      page.__mockLocator.count.mockResolvedValue(0);

      const chatPage = new ChatPage(page as any);
      const result = await chatPage.isStreaming();

      expect(result).toBe(false);
    });
  });

  describe('waitForSocketConnected', () => {
    it('calls waitForFunction with correct check', async () => {
      const chatPage = new ChatPage(page as any);
      await chatPage.waitForSocketConnected();

      expect(page.waitForFunction).toHaveBeenCalledWith(
        expect.any(Function),
        { timeout: 10_000 }
      );
    });
  });
});
