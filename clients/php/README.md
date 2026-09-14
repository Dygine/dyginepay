# PHP client

Drop `DyginePay.php` into your project.

```php
require_once 'DyginePay.php';
$pay = new DyginePay(getenv('DYGINE_KEY_ID'), getenv('DYGINE_KEY_SECRET'));
```

### Laravel

Bind it once in `AppServiceProvider::register()`:

```php
$this->app->singleton(DyginePay::class, fn () => new DyginePay(
    config('services.dygine.key_id'),
    config('services.dygine.key_secret'),
    config('services.dygine.base_url', 'https://pay.dygine.com')
));
```

Then exclude the webhook route from CSRF in `VerifyCsrfToken`:

```php
protected $except = ['dygine/webhook'];
```

CSRF protection assumes a browser session. A server-to-server webhook has none,
so leaving it on rejects every event with a 419 — and the signature check is the
real protection here anyway.
