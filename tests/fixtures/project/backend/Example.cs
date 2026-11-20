using Microsoft.EntityFrameworkCore;

namespace FormatterBench;

public class ExampleContext(DbContextOptions<ExampleContext> options) : DbContext(options)
{
    public DbSet<Group> Groups => Set<Group>();
    public DbSet<Item> Items => Set<Item>();

    protected override void OnModelCreating(ModelBuilder modelBuilder)
    {
        modelBuilder.Entity<Group>()
            .HasMany(group => group.Items)
            .WithOne()
            .HasForeignKey(item => item.GroupId)
            .OnDelete(DeleteBehavior.Cascade);
    }
}

public class Group
{
    public int Id { get; set; }
    public List<Item> Items { get; set; } = [];
}

public class Item
{
    public int Id { get; set; }
    public int GroupId { get; set; }
    public string Name { get; set; } = "";
}
