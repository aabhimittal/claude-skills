# Example: a Rails migration with operations that lock a large table.
# Run: analyze_migration.py unsafe_rails_migration.rb

class AddIndexesAndColumns < ActiveRecord::Migration[7.1]
  def change
    # Builds the index with a lock that blocks writes for the whole build.
    add_index :users, :email

    # NOT NULL with no default rewrites the table under a lock.
    add_column :users, :active, :boolean, null: false

    # Destructive: permanently drops the column's data.
    remove_column :users, :legacy_token
  end
end

# Safer version:
#
#   class AddIndexesAndColumns < ActiveRecord::Migration[7.1]
#     disable_ddl_transaction!
#     def change
#       add_index :users, :email, algorithm: :concurrently
#       add_column :users, :active, :boolean, null: false, default: false
#       # drop legacy_token in a later migration, after code stops using it
#     end
#   end
