import { Page, expect } from '@playwright/test';

export class BasePage {
  constructor(protected page: Page) {}

  async goto(path: string = '/'): Promise<void> {
    await this.page.goto(path);
  }

  async waitForAppReady(): Promise<void> {
    await this.page.waitForLoadState('networkidle');
  }

  async getTitle(): Promise<string> {
    return this.page.title();
  }

  async hasElement(selector: string): Promise<boolean> {
    const element = this.page.locator(selector);
    return (await element.count()) > 0;
  }

  async waitForNavigation(url?: string | RegExp): Promise<void> {
    if (url) {
      await this.page.waitForURL(url);
    } else {
      await this.page.waitForLoadState('domcontentloaded');
    }
  }

  async getCookie(name: string): Promise<string | undefined> {
    const cookies = await this.page.context().cookies();
    const cookie = cookies.find((c) => c.name === name);
    return cookie?.value;
  }

  async isAuthenticated(): Promise<boolean> {
    const sessionCookie = await this.getCookie('elitea_staging_auth_session');
    return sessionCookie !== undefined;
  }
}
