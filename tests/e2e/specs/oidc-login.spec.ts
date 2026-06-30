import { test, expect } from '@playwright/test';
import { LoginPage } from '../pages/LoginPage';

test.describe('OIDC Mock Login Flow', () => {
  test('redirects unauthenticated users to OIDC mock', async ({ page }) => {
    await page.goto('/');
    await page.waitForURL(/oidc-mock\.technicaldomain\.xyz|forward-auth\/login/, {
      timeout: 15_000,
    });

    const url = page.url();
    expect(
      url.includes('oidc-mock.technicaldomain.xyz') ||
      url.includes('forward-auth/login')
    ).toBe(true);
  });

  test('completes login via OIDC mock provider', async ({ page }) => {
    const loginPage = new LoginPage(page);

    await loginPage.completeOidcLogin('testuser', 'testpass');
    await loginPage.verifyLoggedIn();

    // Should be on the app now
    const url = page.url();
    expect(url).toContain('elitea-staging.technicaldomain.xyz');
  });

  test('session cookie is set after login', async ({ page }) => {
    const loginPage = new LoginPage(page);
    await loginPage.completeOidcLogin('testuser', 'testpass');

    const cookies = await page.context().cookies();
    const sessionCookie = cookies.find(
      (c) => c.name.includes('elitea') && c.name.includes('session')
    );

    expect(sessionCookie).toBeDefined();
    expect(sessionCookie!.httpOnly).toBe(true);
    expect(sessionCookie!.secure).toBe(true);
  });

  test('session persists across page navigation', async ({ page }) => {
    const loginPage = new LoginPage(page);
    await loginPage.completeOidcLogin('testuser', 'testpass');

    // Navigate away and back
    await page.goto('/');
    await page.waitForLoadState('networkidle');

    // Should NOT be redirected to login
    const url = page.url();
    expect(url).not.toContain('oidc-mock');
    expect(url).not.toContain('login');
  });

  test('multiple concurrent logins work independently', async ({ browser }) => {
    const context1 = await browser.newContext({ ignoreHTTPSErrors: true });
    const context2 = await browser.newContext({ ignoreHTTPSErrors: true });

    const page1 = await context1.newPage();
    const page2 = await context2.newPage();

    const login1 = new LoginPage(page1);
    const login2 = new LoginPage(page2);

    await Promise.all([
      login1.completeOidcLogin('user1', 'pass1'),
      login2.completeOidcLogin('user2', 'pass2'),
    ]);

    await login1.verifyLoggedIn();
    await login2.verifyLoggedIn();

    await context1.close();
    await context2.close();
  });
});
