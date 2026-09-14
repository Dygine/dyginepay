<?php
/**
 * Dygine Pay client for PHP tools (Laravel, CodeIgniter, plain PHP).
 *
 * No dependencies beyond curl. Drop this file in and require it.
 *
 *   $pay = new DyginePay('dgn_live_xxx', 'secret', 'https://pay.dygine.com');
 *   $session = $pay->createCheckout([
 *       'customer'    => ['external_id' => $org->id, 'name' => $org->name,
 *                         'state_code' => '29'],
 *       'purpose'     => 'subscription',
 *       'plan_code'   => 'pro',
 *       'success_url' => 'https://app.example.in/billing/done',
 *   ], "sub-{$org->id}-{$period}");
 *   header('Location: ' . $session['checkout_url']);
 */

class DyginePayError extends Exception
{
    public string $errorCode;

    public function __construct(string $message, int $status = 0, string $code = '')
    {
        parent::__construct($message, $status);
        $this->errorCode = $code;
    }
}

class DyginePay
{
    private string $baseUrl;
    private string $auth;
    private int $timeout;

    public function __construct(string $keyId, string $keySecret,
                                string $baseUrl = 'https://pay.dygine.com',
                                int $timeout = 20)
    {
        $this->baseUrl = rtrim($baseUrl, '/');
        $this->auth = base64_encode("{$keyId}:{$keySecret}");
        $this->timeout = $timeout;
    }

    private function call(string $method, string $path, ?array $body = null,
                          ?string $idempotencyKey = null): array
    {
        $headers = [
            'Authorization: Basic ' . $this->auth,
            'Content-Type: application/json',
        ];
        if ($idempotencyKey !== null) {
            $headers[] = 'Idempotency-Key: ' . $idempotencyKey;
        }

        $ch = curl_init($this->baseUrl . $path);
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_CUSTOMREQUEST  => $method,
            CURLOPT_HTTPHEADER     => $headers,
            CURLOPT_TIMEOUT        => $this->timeout,
        ]);
        if ($body !== null) {
            curl_setopt($ch, CURLOPT_POSTFIELDS, json_encode($body));
        }

        $raw    = curl_exec($ch);
        $status = curl_getinfo($ch, CURLINFO_HTTP_CODE);
        $err    = curl_error($ch);
        curl_close($ch);

        if ($raw === false) {
            throw new DyginePayError("Could not reach Dygine Pay: {$err}");
        }

        $data = json_decode($raw, true) ?: [];

        if ($status >= 400) {
            $e = $data['error'] ?? [];
            throw new DyginePayError($e['message'] ?? $raw, $status,
                                     $e['code'] ?? '');
        }
        return $data;
    }

    /**
     * Create a payment and get a URL to redirect the customer to.
     *
     * Always pass a stable $idempotencyKey. Without one, a double-clicked
     * button creates two orders and can charge the customer twice.
     */
    public function createCheckout(array $payload, ?string $idempotencyKey = null): array
    {
        return $this->call('POST', '/v1/checkout/sessions', $payload,
            $idempotencyKey ?? ('auto-' . bin2hex(random_bytes(12))));
    }

    /** Safe to call any time. Your self-heal path for a missed webhook. */
    public function getPayment(string $reference): array
    {
        return $this->call('GET', "/v1/payments/{$reference}");
    }

    public function listPayments(?string $customerExternalId = null, int $limit = 50): array
    {
        $q = "?limit={$limit}";
        if ($customerExternalId) {
            $q .= '&customer_external_id=' . urlencode($customerExternalId);
        }
        return $this->call('GET', "/v1/payments{$q}")['data'];
    }

    public function refund(string $reference, ?int $amountPaise = null,
                           string $reason = ''): array
    {
        $body = ['reason' => $reason];
        if ($amountPaise !== null) {
            $body['amount'] = $amountPaise;
        }
        return $this->call('POST', "/v1/payments/{$reference}/refund", $body);
    }

    public function upsertCustomer(array $fields): array
    {
        return $this->call('POST', '/v1/customers', $fields);
    }

    public function getCustomer(string $externalId): array
    {
        return $this->call('GET', "/v1/customers/{$externalId}");
    }

    public function getWallet(string $customerExternalId): array
    {
        return $this->call('GET', "/v1/customers/{$customerExternalId}/wallet");
    }

    /**
     * Consume wallet balance.
     *
     * Throws DyginePayError with errorCode 'insufficient_balance' when there is
     * not enough. Catch that specifically and prompt a top-up rather than
     * showing a generic failure.
     *
     * $idempotencyKey should identify what is being charged for, not the
     * attempt — 'sms-batch-4417', not a fresh random string.
     */
    public function debitWallet(string $customerExternalId, int $amountPaise,
                                string $description = '',
                                ?string $idempotencyKey = null): array
    {
        return $this->call('POST', '/v1/wallet/debit', [
            'customer_external_id' => $customerExternalId,
            'amount'               => $amountPaise,
            'description'          => $description,
        ], $idempotencyKey);
    }

    public function getInvoice(string $invoiceId): array
    {
        return $this->call('GET', "/v1/invoices/{$invoiceId}");
    }

    public function invoicePdfUrl(string $invoiceId): string
    {
        return "{$this->baseUrl}/v1/invoices/{$invoiceId}/pdf";
    }

    public function listInvoices(string $customerExternalId): array
    {
        return $this->call('GET', "/v1/customers/{$customerExternalId}/invoices")['data'];
    }

    public function getSubscription(string $reference): array
    {
        return $this->call('GET', "/v1/subscriptions/{$reference}");
    }

    public function listSubscriptions(string $customerExternalId): array
    {
        return $this->call('GET', "/v1/customers/{$customerExternalId}/subscriptions")['data'];
    }

    public function cancelSubscription(string $reference): array
    {
        return $this->call('POST', "/v1/subscriptions/{$reference}/cancel");
    }

    public function listPlans(): array
    {
        return $this->call('GET', '/v1/plans')['data'];
    }

    /**
     * Verify an inbound Dygine Pay webhook.
     *
     * Pass the RAW request body — file_get_contents('php://input') — not a
     * decoded and re-encoded copy. Re-encoding changes the bytes and the
     * signature will never match.
     *
     *   $raw = file_get_contents('php://input');
     *   $sig = $_SERVER['HTTP_X_DYGINE_SIGNATURE'] ?? '';
     *   if (!DyginePay::verifyWebhook($secret, $raw, $sig)) { http_response_code(403); exit; }
     */
    public static function verifyWebhook(string $secret, string $rawBody,
                                         string $signatureHeader): bool
    {
        $expected = 'sha256=' . hash_hmac('sha256', $rawBody, $secret);
        return hash_equals($expected, trim($signatureHeader));
    }
}
