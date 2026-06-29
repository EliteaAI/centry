import { test as base } from '@playwright/test';
import { KubernetesHelper } from '../utils/kubernetes';

type K8sFixtures = {
  k8s: KubernetesHelper;
};

export const test = base.extend<K8sFixtures>({
  k8s: async ({}, use) => {
    const helper = new KubernetesHelper(process.env.K8S_NAMESPACE || 'elitea-staging');
    await use(helper);
  },
});

export { expect } from '@playwright/test';
