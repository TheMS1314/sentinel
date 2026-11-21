import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from '../api';
afterEach(() => vi.restoreAllMocks());

describe('session API', () => {
  it('uses cookies and CSRF headers without embedding a service key', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue({ok:true,json:async()=>({subject:'owner'})});
    await api.me();
    const [, options] = fetch.mock.calls[0];
    expect(options.credentials).toBe('include');
    expect(options.headers['X-Sentinel-CSRF']).toBe('1');
    expect(options.headers['X-API-Key']).toBeUndefined();
  });
  it('signals expired sessions and preserves useful API errors', async () => {
    const listener=vi.fn(); window.addEventListener('sentinel:unauthorized',listener);
    vi.spyOn(globalThis,'fetch').mockResolvedValue({ok:false,status:401,json:async()=>({error:{message:'Sign in required'}})});
    await expect(api.me()).rejects.toThrow('Sign in required');
    expect(listener).toHaveBeenCalledOnce();
    window.removeEventListener('sentinel:unauthorized',listener);
  });
});
