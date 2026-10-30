---
name: mongodb
description: Inferring MongoDB schemas from samples, aggregation pipelines, BSON type mismatches and pymongo-to-pandas
platforms: [mongodb, python]
---
**Inventory**
- mongosh: `show dbs`, `use mydb`, `show collections`, `db.c.countDocuments({})`, `db.c.findOne()`, `db.c.getIndexes()`.
- pymongo: `client.list_database_names()`, `db.list_collection_names()`, `db.c.count_documents({})`.
- `estimatedDocumentCount()` is metadata-based; use `countDocuments(filter)` for answers.

**Infer the schema from a sample (fields vary per document)**
```js
db.c.aggregate([
  {$sample: {size: 2000}},
  {$project: {kv: {$objectToArray: "$$ROOT"}}},
  {$unwind: "$kv"},
  {$group: {_id: {k: "$kv.k", t: {$type: "$kv.v"}}, n: {$sum: 1}}},
  {$sort: {"_id.k": 1}}
])
```
- A field with several types (`string` and `double`, `date` and `string`) needs conversion before filtering or summing.
- Repeat for nested paths (`$objectToArray: "$addr"`) and inspect arrays with `{$size: "$items"}`.
- Missing field vs `null`: `{f: null}` matches both; `{f: {$exists: false}}` only missing; `{f: {$type: "null"}}` only null.

**find vs aggregate**
- `find(filter, projection)` for simple lookups: `db.c.find({status: "A", "addr.city": "Paris"}, {_id: 0, name: 1}).limit(5)`.
- `aggregate` for joins, reshaping and grouping. Put `$match` first so indexes apply.
- Filtering on an array field matches if ANY element matches; use `$elemMatch` for several conditions on one element.
- Compare two fields: `{$expr: {$gt: ["$spent", "$budget"]}}`.

**Pipeline stages**
- `{$match: {...}}`, `{$project: {_id: 0, a: 1, b: "$x.y"}}`, `{$addFields: {...}}` / `{$set: {...}}`.
- `{$unwind: {path: "$items", preserveNullAndEmptyArrays: true}}` - multiplies documents (fan-out); recount after it.
- `{$group: {_id: "$k", n: {$sum: 1}, total: {$sum: "$amt"}, avg: {$avg: "$amt"}, ids: {$addToSet: "$id"}}}`; `_id: null` for a grand total.
- `{$lookup: {from: "other", localField: "cust_id", foreignField: "_id", as: "cust"}}` returns an ARRAY; follow with `$unwind` or `{$first: "$cust"}`.
- `$sort`, `$limit`, `$count: "n"`, `$facet`, `$bucket`. Large groups: `aggregate(pipeline, {allowDiskUse: true})`.

**Type mismatches (silent wrong results)**
- Queries match only the same BSON type class: `{price: 10}` does not match `"10"`; a Date does not match `"2024-01-01"`.
- `_id`/foreign keys may be ObjectId in one collection and string in another: convert with `{$toString: "$_id"}` or `{$toObjectId: "$ref"}`. pymongo: `from bson import ObjectId; {"_id": ObjectId(s)}`.
- `$sum`/`$avg` IGNORE non-numeric values (strings count as nothing). Convert first.
- Safe conversion: `{$convert: {input: "$amt", to: "double", onError: null, onNull: null}}`; also `$toDouble`, `$toInt`, `$toDecimal`, `$toDate`.
- Strings to dates: `{$dateFromString: {dateString: "$d", format: "%Y-%m-%d", onError: null}}`.
- Date parts: `$year`, `$month`, `{$dateToString: {format: "%Y-%m", date: "$d"}}`, `$dateTrunc` and `$dateDiff` (5.0+). Dates are stored in UTC; pass `timezone` when bucketing by local day.
- Text match: `{name: {$regex: "^acme", $options: "i"}}`; string equality matches exact case.

**Counting distinct**
- `db.c.distinct("f", filter).length` (result must fit 16 MB; array fields return each element).
- Scalable: `[{$group: {_id: "$f"}}, {$count: "n"}]`. Check that `null`/missing are not counted as a value unless intended.

**pymongo to pandas**
```python
from pymongo import MongoClient
import pandas as pd
db = MongoClient(uri)["mydb"]
docs = list(db.c.find({"status": "A"}, {"_id": 1, "amt": 1, "addr": 1}))
df = pd.json_normalize(docs, sep=".")   # nested -> addr.city
df["_id"] = df["_id"].astype(str)
```
- Push filtering/grouping into `aggregate` when collections are large; pull only needed fields.
- Datetimes come back naive but in UTC (unless `tz_aware=True`). `Decimal128` values need `.to_decimal()` then `float`.
- Explode arrays in pandas with `df.explode("items")` then `pd.json_normalize(df["items"].tolist())` (reset the index first to align).
- Verify: compare `len(df)` with `count_documents(filter)`; spot-check a few documents by `_id`.
