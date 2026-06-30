import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('@playwright/test', () => ({
  Page: vi.fn(),
  expect: vi.fn().mockReturnValue({ toBe: vi.fn() }),
}));

function createMockPage() {
  const mockLocator = {
    first: vi.fn().mockReturnThis(),
    fill: vi.fn().mockResolvedValue(undefined),
    click: vi.fn().mockResolvedValue(undefined),
    count: vi.fn().mockResolvedValue(0),
    textContent: vi.fn().mockResolvedValue(null),
  };

  return {
    goto: vi.fn().mockResolvedValue(undefined),
    waitForLoadState: vi.fn().mockResolvedValue(undefined),
    waitForURL: vi.fn().mockResolvedValue(undefined),
    title: vi.fn().mockResolvedValue('Elitea'),
    locator: vi.fn().mockReturnValue(mockLocator),
    context: vi.fn().mockReturnValue({
      cookies: vi.fn().mockResolvedValue([]),
    }),
    __mockLocator: mockLocator,
  };
}

import { LoginPage } from './LoginPage';
import { BasePage } from './BasePage';

describe('BasePage', () => {
  let page: ReturnType<typeof createMockPage>;

  beforeEach(() => {
    page = createMockPage();
  });

  describe('goto', () => {
    it('navigates to path', async () => {
      const basePage = new BasePage(page as any);
      await basePage.goto('/test');
      expect(page.goto).toHaveBeenCalledWith('/test');
    });

    it('defaults to root path', async () => {
      const basePage = new BasePage(page as any);
      await basePage.goto();
      expect(page.goto).toHaveBeenCalledWith('/');
    });
  });

  describe('waitForAppReady', () => {
    it('waits for networkidle', async () => {
      const basePage = new BasePage(page as any);
      await basePage.waitForAppReady();
      expect(page.waitForLoadState).toHaveBeenCalledWith('networkidle');
    });
  });

  describe('getTitle', () => {
    it('returns page title', async () => {
      const basePage = new BasePage(page as any);
      const title = await basePage.getTitle();
      expect(title).toBe('Elitea');
    });
  });

  describe('hasElement', () => {
    it('returns true when element exists', async () => {
      page.__mockLocator.count.mockResolvedValue(1);
      const basePage = new BasePage(page as any);
      const result = await basePage.hasElement('.test');
      expect(result).toBe(true);
    });

    it('returns false when element missing', async () => {
      page.__mockLocator.count.mockResolvedValue(0);
      const basePage = new BasePage(page as any);
      const result = await basePage.hasElement('.missing');
      expect(result).toBe(false);
    });
  });

  describe('waitForNavigation', () => {
    it('waits for specific URL when provided', async () => {
      const basePage = new BasePage(page as any);
      await basePage.waitForNavigation(/test/);
      expect(page.waitForURL).toHaveBeenCalledWith(/test/);
    });

    it('waits for domcontentloaded when no URL', async () => {
      const basePage = new BasePage(page as any);
      await basePage.waitForNavigation();
      expect(page.waitForLoadState).toHaveBeenCalledWith('domcontentloaded');
    });
  });

  describe('getCookie', () => {
    it('returns cookie value when found', async () => {
      page.context.mockReturnValue({
        cookies: vi.fn().mockResolvedValue([
          { name: 'session', value: 'abc123' },
          { name: 'other', value: 'xyz' },
        ]),
      });

      const basePage = new BasePage(page as any);
      const result = await basePage.getCookie('session');
      expect(result).toBe('abc123');
    });

    it('returns undefined when cookie not found', async () => {
      page.context.mockReturnValue({
        cookies: vi.fn().mockResolvedValue([]),
      });

      const basePage = new BasePage(page as any);
      const result = await basePage.getCookie('missing');
      expect(result).toBeUndefined();
    });
  });

  describe('isAuthenticated', () => {
    it('returns true when session cookie exists', async () => {
      page.context.mockReturnValue({
        cookies: vi.fn().mockResolvedValue([
          { name: 'elitea_staging_auth_session', value: 'session-data' },
        ]),
      });

      const basePage = new BasePage(page as any);
      const result = await basePage.isAuthenticated();
      expect(result).toBe(true);
    });

    it('returns false when session cookie missing', async () => {
      page.context.mockReturnValue({
        cookies: vi.fn().mockResolvedValue([]),
      });

      const basePage = new BasePage(page as any);
      const result = await basePage.isAuthenticated();
      expect(result).toBe(false);
    });
  });
});

describe('LoginPage', () => {
  let page: ReturnType<typeof createMockPage>;

  beforeEach(() => {
    page = createMockPage();
  });

  describe('navigateToLogin', () => {
    it('goes to root and waits for DOM', async () => {
      const loginPage = new LoginPage(page as any);
      await loginPage.navigateToLogin();

      expect(page.goto).toHaveBeenCalledWith('/');
      expect(page.waitForLoadState).toHaveBeenCalledWith('domcontentloaded');
    });
  });

  describe('completeOidcLogin', () => {
    it('fills credentials and submits', async () => {
      const loginPage = new LoginPage(page as any);
      await loginPage.completeOidcLogin('admin', 'password');

      expect(page.goto).toHaveBeenCalledWith('/');
      expect(page.waitForURL).toHaveBeenCalledWith(/oidc-mock\.technicaldomain\.xyz/, expect.any(Object));
      expect(page.__mockLocator.fill).toHaveBeenCalledWith('admin');
      expect(page.__mockLocator.fill).toHaveBeenCalledWith('password');
      expect(page.__mockLocator.click).toHaveBeenCalled();
      expect(page.waitForURL).toHaveBeenCalledWith(/elitea-staging\.technicaldomain\.xyz/, expect.any(Object));
      expect(page.waitForLoadState).toHaveBeenCalledWith('networkidle');
    });
  });

  describe('verifyLoggedIn', () => {
    it('does not throw when session cookie exists', async () => {
      page.context.mockReturnValue({
        cookies: vi.fn().mockResolvedValue([
          { name: 'elitea_staging_auth_session', value: 'session-data' },
        ]),
      });

      const loginPage = new LoginPage(page as any);
      await expect(loginPage.verifyLoggedIn()).resolves.toBeUndefined();
    });
  });

  describe('getLoggedInUsername', () => {
    it('returns username when element exists', async () => {
      page.__mockLocator.count.mockResolvedValue(1);
      page.__mockLocator.textContent.mockResolvedValue('admin');

      const loginPage = new LoginPage(page as any);
      const username = await loginPage.getLoggedInUsername();
      expect(username).toBe('admin');
    });

    it('returns null when element not found', async () => {
      page.__mockLocator.count.mockResolvedValue(0);

      const loginPage = new LoginPage(page as any);
      const username = await loginPage.getLoggedInUsername();
      expect(username).toBeNull();
    });
  });
});
