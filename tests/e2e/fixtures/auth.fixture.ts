import { test as base, BrowserContext, Page } from '@playwright/test';
import { LoginPage } from '../pages/LoginPage';

type AuthFixtures = {
  authenticatedPage: Page;
  authenticatedContext: BrowserContext;
};

export const test = base.extend<AuthFixtures>({
  authenticatedContext: async ({ browser }, use) => {
    const context = await browser.newContext({
      ignoreHTTPSErrors: true,
    });
    const page = await context.newPage();
    const loginPage = new LoginPage(page);

    await loginPage.completeOidcLogin(
      process.env.TEST_USERNAME || 'testuser',
      process.env.TEST_PASSWORD || 'testpass'
    );

    await page.close();
    await use(context);
    await context.close();
  },

  authenticatedPage: async ({ authenticatedContext }, use) => {
    const page = await authenticatedContext.newPage();
    await use(page);
    await page.close();
  },
});

export { expect } from '@playwright/test';
