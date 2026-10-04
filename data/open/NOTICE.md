# Datasets committed with this repository

The code in this repository is under the PolyForm Noncommercial License (see `LICENSE`). Data keeps the licence it was published under.
Everything else under `data/` is third-party, licensed for use but not redistribution, and
gitignored: fetch it with `scripts/fetch_datasets.py`.

## netflix_customer_churn.csv

- **Title:** Netflix Customer Churn & Engagement Analytics
- **Source:** https://www.kaggle.com/datasets/zeyadmohamed26/netflix-customer-churn-and-engagement-analytics
- **Licence:** CC0 1.0 Universal (public domain dedication), as stated on the Kaggle page. No
  attribution is required; it is given anyway.
- **Rows:** 5,000 subscribers, 14 columns, `churned` 1 = churned. No missing values.
- **Synthetic or real:** the source does not say. A 50% churn rate and a clean behavioural
  signal suggest synthetic, so treat results on it as a demonstration of the pipeline and not
  as evidence about real subscribers. The real-data evidence is KKBox, telecom and bank.
- **Unmodified:** the file is byte-for-byte what Kaggle serves. Cleaning (dropping
  `customer_id`) happens at load time in `agentstack.context.datasets`, with the reason
  recorded there.
- **RevenueCat mapping:** `customer_id` -> `app_user_id`; `subscription_type` -> `product_id`;
  `monthly_fee` -> price (USD); `device` -> platform; `churned` -> `EXPIRATION` with no
  renewal. The file has no dates, so no `purchased_at` or `expiration_at` is produced: it is a
  subscriber snapshot at a cutoff, like the output of `agentstack.context.kkbox.to_features`.
