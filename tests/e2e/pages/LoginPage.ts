import { Page, expect } from '@playwright/test';
import { BasePage } from './BasePage';

export class LoginPage extends BasePage {
  private readonly loginButton = '[data-testid="login-button"], a[href*="login"], button:has-text("Login")';
  private readonly usernameInput = 'input[name="username"], input[name="login"], input[type="email"]';
  private readonly passwordInput = 'input[name="password"], input[type="password"]';
  private readonly submitButton = 'button[type="submit"], input[type="submit"]';

  constructor(page: Page) {
    super(page);
  }

  async navigateToLogin(): Promise<void> {
    await this.goto('/');
    await this.page.waitForLoadState('domcontentloaded');
  }

  async completeOidcLogin(username: string, password: string): Promise<void> {
    await this.navigateToLogin();

    // Wait for redirect to OIDC mock
    await this.page.waitForURL(/oidc-mock\.technicaldomain\.xyz/, { timeout: 15_000 });

    // Fill credentials on OIDC mock login form
    await this.page.locator(this.usernameInput).first().fill(username);
    await this.page.locator(this.passwordInput).first().fill(password);
    await this.page.locator(this.submitButton).first().click();

    // Wait for redirect back to app
    await this.page.waitForURL(/elitea-staging\.technicaldomain\.xyz/, { timeout: 15_000 });
    await this.waitForAppReady();
  }

  async verifyLoggedIn(): Promise<void> {
    const isAuth = await this.isAuthenticated();
    expect(isAuth).toBe(true);
  }

  async getLoggedInUsername(): Promise<string | null> {
    const userElement = this.page.locator('[data-testid="user-name"], .user-name, .username');
    if ((await userElement.count()) > 0) {
      return userElement.first().textContent();
    }
    return null;
  }
}
